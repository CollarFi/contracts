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
