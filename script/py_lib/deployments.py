from __future__ import annotations

import json
from pathlib import Path

from lz_harness.common import ROOT_DIR, must, run


def read_addr_from_output(path_value: str, key: str) -> str:
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


def default_output_json(rpc_url: str, side: str) -> str:
    chain_id = run(["cast", "chain-id", "--rpc-url", rpc_url])
    return str(ROOT_DIR / "deployments" / chain_id / f"{side}.json")


def resolve_addr(env: dict[str, str], env_key: str, out_key: str, side: str) -> str:
    if env.get(env_key):
        return str(env[env_key])
    output_json = env.get("OUTPUT_JSON") or default_output_json(must(env, "RPC_URL"), side)
    return read_addr_from_output(output_json, out_key)


def receiver_from_broadcast(rpc_url: str) -> str:
    chain_id = run(["cast", "chain-id", "--rpc-url", rpc_url])
    run_path = ROOT_DIR / "broadcast" / "DeployL2.s.sol" / str(chain_id) / "run-latest.json"
    if not run_path.is_file():
        raise FileNotFoundError(f"missing L2 broadcast artifact: {run_path}")
    run_json = json.loads(run_path.read_text(encoding="utf-8"))
    txs: list[dict] = run_json.get("transactions", [])
    for tx in txs:
        if tx.get("transactionType") == "CREATE" and tx.get("contractName") == "CollarTSAReceiver":
            addr = tx.get("contractAddress")
            if addr:
                return str(addr)
    raise ValueError(f"CollarTSAReceiver CREATE not found in {run_path}")
