from __future__ import annotations

import json
from pathlib import Path

from lz_harness.common import cast_call, load_env, must
from py_lib.deployments import read_addr_from_output, receiver_from_broadcast


def resolve_l2_receiver(l2_env: dict[str, str]) -> str:
    if l2_env.get("L2_RECEIVER"):
        return str(l2_env["L2_RECEIVER"])

    output_json = l2_env.get("OUTPUT_JSON")
    if output_json:
        out_path = Path(output_json)
        if not out_path.is_absolute():
            from lz_harness.common import ROOT_DIR

            out_path = ROOT_DIR / out_path
        if out_path.is_file():
            data = json.loads(out_path.read_text(encoding="utf-8"))
            addrs = data.get("addrs", data)
            if addrs.get("l2Receiver"):
                return str(addrs["l2Receiver"])

    return receiver_from_broadcast(must(l2_env, "RPC_URL"))


def _resolve_tsa_from_receiver(l2_env_file: Path) -> tuple[dict[str, str], str, str]:
    l2 = load_env(l2_env_file)
    rpc_url = must(l2, "RPC_URL")
    receiver = resolve_l2_receiver(l2)
    tsa = cast_call(rpc_url, receiver, "tsa()(address)")
    return l2, rpc_url, tsa


def resolve_l2_wrapped_asset_from_tsa(l2_env_file: Path) -> str:
    _, rpc_url, tsa = _resolve_tsa_from_receiver(l2_env_file)
    base = cast_call(rpc_url, tsa, "getBaseTSAAddresses()(address,address,address,address,address,address,address)")
    lines = [ln.strip() for ln in base.splitlines() if ln.strip()]
    if len(lines) < 3:
        raise ValueError("failed to parse getBaseTSAAddresses() output from TSA")
    return lines[2]


def resolve_l2_subaccount_id_from_tsa(l2_env_file: Path) -> int:
    _, rpc_url, tsa = _resolve_tsa_from_receiver(l2_env_file)
    value = cast_call(rpc_url, tsa, "subAccount()(uint256)").strip()
    try:
        return int(value.split()[0], 10)
    except Exception as exc:
        raise ValueError(f"failed to parse TSA subAccount() output: {value}") from exc
