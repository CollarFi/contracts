#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich import print

from lz_harness.common import ROOT_DIR, cast_call, cast_send, load_env, must, run

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


def _first_line(s: str) -> str:
    return s.splitlines()[0].strip()


def _must_non_empty_hex(name: str, value: str) -> str:
    v = value.strip().lower()
    if v in {"", "0x", "n/a"}:
        raise ValueError(f"{name} is empty or unavailable: {value}")
    return value.strip()


def _encode_lz_receive_option(gas: int, value: int) -> str:
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

    send_lib = _first_line(cast_call(rpc, endpoint, "getSendLibrary(address,uint32)(address)", oapp, dst_eid))
    recv_lib = _first_line(cast_call(rpc, endpoint, "getReceiveLibrary(address,uint32)(address,bool)", oapp, src_eid))

    send_cfg_exec = _must_non_empty_hex(
        f"{label} send config type1",
        cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, send_lib, dst_eid, "1"),
    )
    send_cfg_uln = _must_non_empty_hex(
        f"{label} send config type2",
        cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, send_lib, dst_eid, "2"),
    )
    recv_cfg_uln = _must_non_empty_hex(
        f"{label} receive config type2",
        cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, recv_lib, src_eid, "2"),
    )

    send_params = f"[({dst_eid},1,{send_cfg_exec}),({dst_eid},2,{send_cfg_uln})]"
    recv_params = f"[({src_eid},2,{recv_cfg_uln})]"

    desired_default_options = None
    if env.get("LZ_RECEIVE_GAS"):
        desired_default_options = _encode_lz_receive_option(
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

    current_remote_eid = cast_call(side["rpc"], oapp, "remoteEid()(uint32)", allow_fail=True)
    current_default_options = cast_call(side["rpc"], oapp, "defaultOptions()(bytes)", allow_fail=True)

    if not broadcast:
        print("  [yellow]dry-run[/yellow] would call:")
        print(f"    setConfig({oapp}, {send_lib}, {send_params})")
        print(f"    setConfig({oapp}, {recv_lib}, {recv_params})")
        if _parse_uint(current_remote_eid) != int(desired_remote_eid):
            print(f"    setRemoteEid({desired_remote_eid}) on {oapp}")
        if desired_default_options and current_default_options.lower() != desired_default_options.lower():
            print(f"    setDefaultOptions({desired_default_options}) on {oapp}")
        return

    cast_send(
        side["rpc"],
        side["account"],
        endpoint,
        sig,
        oapp,
        send_lib,
        send_params,
    )
    cast_send(
        side["rpc"],
        side["account"],
        endpoint,
        sig,
        oapp,
        recv_lib,
        recv_params,
    )

    if _parse_uint(current_remote_eid) != int(desired_remote_eid):
        cast_send(
            side["rpc"],
            side["account"],
            oapp,
            "setRemoteEid(uint32)",
            desired_remote_eid,
        )

    if desired_default_options and current_default_options.lower() != desired_default_options.lower():
        cast_send(
            side["rpc"],
            side["account"],
            oapp,
            "setDefaultOptions(bytes)",
            desired_default_options,
        )

    print("  [green]applied[/green]")


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    broadcast: bool = typer.Option(False, help="Execute onchain txs (default: dry-run)"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    l1_env_file, l2_env_file = _resolve_env_paths(env_profile, l1_env_file, l2_env_file)

    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    for env in (l1, l2):
        for k in ("RPC_URL", "ACCOUNT", "LZ_ENDPOINT"):
            must(env, k)

    l1_chain_eid = _parse_uint(cast_call(l1["RPC_URL"], l1["LZ_ENDPOINT"], "eid()(uint32)"))
    l2_chain_eid = _parse_uint(cast_call(l2["RPC_URL"], l2["LZ_ENDPOINT"], "eid()(uint32)"))
    if l1_chain_eid is None:
        raise ValueError("failed to resolve L1 endpoint eid()")
    if l2_chain_eid is None:
        raise ValueError("failed to resolve L2 endpoint eid()")

    # Route EIDs are opposite endpoint EIDs.
    l1_to_l2_eid = str(l2_chain_eid)
    l2_to_l1_eid = str(l1_chain_eid)

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
