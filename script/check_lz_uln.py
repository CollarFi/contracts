#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich import print

from lz_harness.common import ROOT_DIR, address_to_peer_bytes32, cast_call, load_env, must, run

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


def _is_zero_address(addr: str) -> bool:
    return addr.lower() == "0x0000000000000000000000000000000000000000"


def _is_empty_hex(blob: str) -> bool:
    b = blob.strip().lower()
    return b in {"", "0x"}


def _encode_lz_receive_option(gas: int, value: int) -> str:
    # Matches common encoding from OptionsBuilder.addExecutorLzReceiveOption.
    # For value=0, options are encoded as: 0x0003 | 0x0100 | 0x11 | 0x01 | gas(uint128)
    if value == 0:
        return "0x000301001101" + f"{gas:032x}"
    return "0x000301001102" + f"{gas:032x}" + f"{value:032x}"


def _parse_uint(value: str) -> int | None:
    s = value.strip()
    if not s or s == "N/A":
        return None
    token = s.split()[0]
    try:
        return int(token)
    except ValueError:
        return None


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

    send_cfg_1 = cast_call(
        rpc_url,
        endpoint,
        "getConfig(address,address,uint32,uint32)(bytes)",
        oapp,
        send_lib,
        remote_eid,
        "1",
        allow_fail=True,
    ) if send_lib != "N/A" else "N/A"
    send_cfg_2 = cast_call(
        rpc_url,
        endpoint,
        "getConfig(address,address,uint32,uint32)(bytes)",
        oapp,
        send_lib,
        remote_eid,
        "2",
        allow_fail=True,
    ) if send_lib != "N/A" else "N/A"

    recv_cfg_2 = cast_call(
        rpc_url,
        endpoint,
        "getConfig(address,address,uint32,uint32)(bytes)",
        oapp,
        recv_lib,
        source_eid,
        "2",
        allow_fail=True,
    ) if recv_lib != "N/A" else "N/A"

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
            "hint": "Run script/wire_lz_peers.py --broadcast if mismatch.",
        }
    )
    checks.append(
        {
            "name": "remoteEid set",
            "ok": _parse_uint(configured_remote_eid) == int(expected_remote_eid),
            "actual": configured_remote_eid,
            "expected": str(expected_remote_eid),
            "hint": "Set setRemoteEid(...) on the OApp contract.",
        }
    )
    checks.append(
        {
            "name": "defaultOptions set",
            "ok": default_options not in {"N/A"} and not _is_empty_hex(default_options),
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
            "ok": delegate != "N/A" and not _is_zero_address(delegate),
            "actual": delegate,
            "hint": "Set OApp delegate via endpoint.setDelegate(...) if zero.",
        }
    )
    checks.append(
        {
            "name": "send library set",
            "ok": send_lib != "N/A" and not _is_zero_address(send_lib),
            "actual": send_lib,
            "hint": "Missing send lib route config on endpoint.",
        }
    )
    checks.append(
        {
            "name": "receive library set",
            "ok": recv_lib != "N/A" and not _is_zero_address(recv_lib),
            "actual": recv_lib,
            "hint": "Missing receive lib route config on endpoint.",
        }
    )

    checks.append(
        {
            "name": "send ULN config (type 1) present",
            "ok": send_cfg_1 not in {"N/A"} and not _is_empty_hex(send_cfg_1),
            "actual": send_cfg_1,
            "hint": "Likely missing ULN config (DVN/confirmations) for send path.",
        }
    )
    checks.append(
        {
            "name": "send Executor config (type 2) present",
            "ok": send_cfg_2 not in {"N/A"} and not _is_empty_hex(send_cfg_2),
            "actual": send_cfg_2,
            "hint": "Likely missing executor config for send path.",
        }
    )
    checks.append(
        {
            "name": "receive ULN config (type 2) present",
            "ok": recv_cfg_2 not in {"N/A"} and not _is_empty_hex(recv_cfg_2),
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
    l1_env_file, l2_env_file = _resolve_env_paths(env_profile, l1_env_file, l2_env_file)

    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    for env in (l1, l2):
        for k in ("RPC_URL", "LZ_ENDPOINT"):
            must(env, k)

    l1_to_l2_eid = l1.get("L2_EID") or l1.get("REMOTE_EID")
    l2_to_l1_eid = l2.get("L1_EID") or l2.get("REMOTE_EID")
    if not l1_to_l2_eid:
        raise ValueError("missing L2_EID in L1 env (or legacy REMOTE_EID)")
    if not l2_to_l1_eid:
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

    l1_expected_peer = address_to_peer_bytes32(l2_receiver)
    l2_expected_peer = address_to_peer_bytes32(l1_messenger)

    l1_expected_options = None
    if l1.get("LZ_RECEIVE_GAS"):
        l1_expected_options = _encode_lz_receive_option(
            int(l1["LZ_RECEIVE_GAS"]),
            int(l1.get("LZ_RECEIVE_VALUE", "0") or 0),
        )

    l2_expected_options = None
    if l2.get("LZ_RECEIVE_GAS"):
        l2_expected_options = _encode_lz_receive_option(
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
