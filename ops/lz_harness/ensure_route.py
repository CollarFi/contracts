#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich import print

from common import ROOT_DIR, address_to_peer_bytes32, cast_call, cast_send, load_env, load_harness_address, must

app = typer.Typer(add_completion=False)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _first_line(value: str) -> str:
    return value.splitlines()[0].strip() if value.strip() else ""


def _norm_hex(value: str) -> str:
    return value.strip().lower()


def _is_zero_address(value: str) -> bool:
    return _norm_hex(value) == ZERO_ADDRESS


def _parse_uint(name: str, value: str) -> int:
    token = _first_line(value).split()[0]
    try:
        return int(token)
    except Exception as exc:
        raise ValueError(f"failed to parse {name} from: {value}") from exc


def _encode_lz_receive_option(gas: int, value: int) -> str:
    if value == 0:
        return "0x000301001101" + f"{gas:032x}"
    return "0x000301001102" + f"{gas:032x}" + f"{value:032x}"


def _must_non_empty_hex(name: str, value: str) -> str:
    normalized = _norm_hex(value)
    if normalized in {"", "0x", "n/a"}:
        raise ValueError(f"{name} is empty or unavailable: {value}")
    return value.strip()


def _collect_side(
    *,
    label: str,
    env: dict[str, str],
    harness: str,
    remote_harness: str,
    desired_remote_eid: str,
    src_eid: str,
) -> dict[str, Any]:
    rpc = env["RPC_URL"]
    endpoint = env["LZ_ENDPOINT"]

    expected_peer = address_to_peer_bytes32(remote_harness)

    current_remote_eid = _first_line(cast_call(rpc, harness, "remoteEid()(uint32)", allow_fail=True))
    current_peer = _first_line(cast_call(rpc, harness, "peers(uint32)(bytes32)", desired_remote_eid, allow_fail=True))
    owner = _first_line(cast_call(rpc, harness, "owner()(address)", allow_fail=True))
    current_delegate = _first_line(cast_call(rpc, endpoint, "delegates(address)(address)", harness, allow_fail=True))
    current_default_options = _first_line(cast_call(rpc, harness, "defaultOptions()(bytes)", allow_fail=True))

    desired_default_options = None
    receive_gas = (env.get("LZ_RECEIVE_GAS") or "").strip()
    if receive_gas:
        receive_value = int((env.get("LZ_RECEIVE_VALUE") or "0").strip() or 0)
        desired_default_options = _encode_lz_receive_option(int(receive_gas), receive_value)

    send_lib = _first_line(cast_call(rpc, endpoint, "getSendLibrary(address,uint32)(address)", harness, desired_remote_eid))
    receive_lib = _first_line(cast_call(rpc, endpoint, "getReceiveLibrary(address,uint32)(address,bool)", harness, src_eid))

    desired_send_cfg_1 = _must_non_empty_hex(
        f"{label} desired send config type1",
        _first_line(cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", endpoint, send_lib, desired_remote_eid, "1")),
    )
    desired_send_cfg_2 = _must_non_empty_hex(
        f"{label} desired send config type2",
        _first_line(cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", endpoint, send_lib, desired_remote_eid, "2")),
    )
    desired_recv_cfg_2 = _must_non_empty_hex(
        f"{label} desired receive config type2",
        _first_line(cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", endpoint, receive_lib, src_eid, "2")),
    )

    current_send_cfg_1 = _first_line(
        cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", harness, send_lib, desired_remote_eid, "1")
    )
    current_send_cfg_2 = _first_line(
        cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", harness, send_lib, desired_remote_eid, "2")
    )
    current_recv_cfg_2 = _first_line(
        cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", harness, receive_lib, src_eid, "2")
    )

    send_params = f"[({desired_remote_eid},1,{desired_send_cfg_1}),({desired_remote_eid},2,{desired_send_cfg_2})]"
    recv_params = f"[({src_eid},2,{desired_recv_cfg_2})]"

    checks = {
        "remoteEid": _parse_uint(f"{label} remoteEid", current_remote_eid) != int(desired_remote_eid),
        "peer": _norm_hex(current_peer) != _norm_hex(expected_peer),
        "defaultOptions": bool(desired_default_options)
        and _norm_hex(current_default_options) != _norm_hex(desired_default_options),
        "delegate": _is_zero_address(current_delegate),
        "sendConfig": _norm_hex(current_send_cfg_1) != _norm_hex(desired_send_cfg_1)
        or _norm_hex(current_send_cfg_2) != _norm_hex(desired_send_cfg_2),
        "receiveConfig": _norm_hex(current_recv_cfg_2) != _norm_hex(desired_recv_cfg_2),
    }

    return {
        "label": label,
        "rpc": rpc,
        "endpoint": endpoint,
        "account": env.get("ACCOUNT", ""),
        "harness": harness,
        "owner": owner,
        "desiredRemoteEid": desired_remote_eid,
        "srcEid": src_eid,
        "currentRemoteEid": current_remote_eid,
        "expectedPeer": expected_peer,
        "currentPeer": current_peer,
        "currentDelegate": current_delegate,
        "currentDefaultOptions": current_default_options,
        "desiredDefaultOptions": desired_default_options,
        "sendLib": send_lib,
        "receiveLib": receive_lib,
        "sendParams": send_params,
        "receiveParams": recv_params,
        "checks": checks,
    }


def _act(side: dict[str, Any], broadcast: bool, emit_logs: bool) -> dict[str, Any]:
    label = side["label"]
    rpc = side["rpc"]
    account = side["account"]
    endpoint = side["endpoint"]
    harness = side["harness"]

    checks = side["checks"]
    actions: list[dict[str, Any]] = []

    def record(setting: str, needs_apply: bool, detail: str, apply_fn: Any | None = None) -> None:
        status = "apply" if needs_apply else "skip"
        tag = "\\[apply]" if needs_apply else "\\[skip]"
        line = f"  {tag} {setting}: {detail}"
        if needs_apply and not broadcast:
            line += " (dry-run)"
        if emit_logs:
            print(line)

        tx_sent = False
        if needs_apply and broadcast:
            if not account:
                raise ValueError(f"{label} requires ACCOUNT in env when --broadcast is used")
            if apply_fn is None:
                raise ValueError(f"missing apply function for {setting}")
            apply_fn()
            tx_sent = True

        actions.append({"setting": setting, "status": status, "txSent": tx_sent, "detail": detail})

    if emit_logs:
        print(f"[bold]{label}[/bold]")

    record(
        "remoteEid",
        checks["remoteEid"],
        f"current={side['currentRemoteEid']} desired={side['desiredRemoteEid']}",
        lambda: cast_send(
            rpc,
            account,
            harness,
            "setRemoteEid(uint32)",
            side["desiredRemoteEid"],
        ),
    )

    record(
        "peer",
        checks["peer"],
        f"current={side['currentPeer']} desired={side['expectedPeer']} routeEid={side['desiredRemoteEid']}",
        lambda: cast_send(
            rpc,
            account,
            harness,
            "setPeer(uint32,bytes32)",
            side["desiredRemoteEid"],
            side["expectedPeer"],
        ),
    )

    if side["desiredDefaultOptions"]:
        record(
            "defaultOptions",
            checks["defaultOptions"],
            f"current={side['currentDefaultOptions']} desired={side['desiredDefaultOptions']}",
            lambda: cast_send(
                rpc,
                account,
                harness,
                "setDefaultOptions(bytes)",
                side["desiredDefaultOptions"],
            ),
        )
    else:
        record("defaultOptions", False, "no LZ_RECEIVE_GAS provided in env")

    record(
        "endpoint.delegate",
        checks["delegate"],
        f"current={side['currentDelegate']} owner={side['owner']}",
        lambda: cast_send(
            rpc,
            account,
            endpoint,
            "setDelegate(address,address)",
            harness,
            side["owner"],
        ),
    )

    record(
        "ULN send config (type1+type2)",
        checks["sendConfig"],
        f"lib={side['sendLib']} dstEid={side['desiredRemoteEid']}",
        lambda: cast_send(
            rpc,
            account,
            endpoint,
            "setConfig(address,address,(uint32,uint32,bytes)[])",
            harness,
            side["sendLib"],
            side["sendParams"],
        ),
    )

    record(
        "ULN receive config (type2)",
        checks["receiveConfig"],
        f"lib={side['receiveLib']} srcEid={side['srcEid']}",
        lambda: cast_send(
            rpc,
            account,
            endpoint,
            "setConfig(address,address,(uint32,uint32,bytes)[])",
            harness,
            side["receiveLib"],
            side["receiveParams"],
        ),
    )

    needs_apply = sum(1 for a in actions if a["status"] == "apply")
    tx_sent = sum(1 for a in actions if a["txSent"])
    if emit_logs:
        print()

    return {
        "label": label,
        "actions": actions,
        "needsApply": needs_apply,
        "txSent": tx_sent,
    }


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    broadcast: bool = typer.Option(False, "--broadcast", help="Execute onchain txs (default: dry-run)"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    for env in (l1, l2):
        for key in ("RPC_URL", "LZ_ENDPOINT", "OUTPUT_JSON"):
            must(env, key)

    l1_harness = load_harness_address(l1["OUTPUT_JSON"])
    l2_harness = load_harness_address(l2["OUTPUT_JSON"])

    l1_endpoint_eid = _parse_uint("L1 endpoint eid", cast_call(l1["RPC_URL"], l1["LZ_ENDPOINT"], "eid()(uint32)"))
    l2_endpoint_eid = _parse_uint("L2 endpoint eid", cast_call(l2["RPC_URL"], l2["LZ_ENDPOINT"], "eid()(uint32)"))

    # Route EIDs are opposite endpoint EIDs.
    l1_to_l2_eid = str(l2_endpoint_eid)
    l2_to_l1_eid = str(l1_endpoint_eid)

    l1_side = _collect_side(
        label="L1 harness",
        env=l1,
        harness=l1_harness,
        remote_harness=l2_harness,
        desired_remote_eid=l1_to_l2_eid,
        src_eid=l1_to_l2_eid,
    )
    l2_side = _collect_side(
        label="L2 harness",
        env=l2,
        harness=l2_harness,
        remote_harness=l1_harness,
        desired_remote_eid=l2_to_l1_eid,
        src_eid=l2_to_l1_eid,
    )

    if not json_out:
        mode = "broadcast" if broadcast else "dry-run"
        print(f"[bold]Ensuring LZ harness route config[/bold] ({mode})")
        print(f"  L1 harness: {l1_harness}")
        print(f"  L2 harness: {l2_harness}")
        print(f"  endpoint EIDs: L1={l1_endpoint_eid}, L2={l2_endpoint_eid}")
        print(f"  route EIDs: L1->L2={l1_to_l2_eid}, L2->L1={l2_to_l1_eid}")
        print()

    l1_result = _act(l1_side, broadcast, emit_logs=not json_out)
    l2_result = _act(l2_side, broadcast, emit_logs=not json_out)

    total_needs_apply = l1_result["needsApply"] + l2_result["needsApply"]
    total_tx_sent = l1_result["txSent"] + l2_result["txSent"]

    summary = {
        "mode": "broadcast" if broadcast else "dry-run",
        "l1Env": str(l1_env_file),
        "l2Env": str(l2_env_file),
        "l1Harness": l1_harness,
        "l2Harness": l2_harness,
        "l1EndpointEid": str(l1_endpoint_eid),
        "l2EndpointEid": str(l2_endpoint_eid),
        "l1ToL2Eid": l1_to_l2_eid,
        "l2ToL1Eid": l2_to_l1_eid,
        "results": [l1_result, l2_result],
        "needsApply": total_needs_apply,
        "txSent": total_tx_sent,
    }

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        return

    if broadcast:
        print(f"\\[done] tx sent: {total_tx_sent}, remaining mismatches: {total_needs_apply - total_tx_sent}")
    else:
        print(f"\\[dry-run] mismatches detected: {total_needs_apply}, tx sent: 0")


if __name__ == "__main__":
    app()
