#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import ROOT_DIR, cast_call, forge_script, load_env, must, require_cmd, resolve_output_json, run

app = typer.Typer(add_completion=False)


def _resolve_env_paths(env_profile: str, l1_env_file: Path, l2_env_file: Path) -> tuple[Path, Path]:
    profile = env_profile.strip().lower()
    if profile and l1_env_file == (ROOT_DIR / ".env.l1.testnet"):
        l1_env_file = ROOT_DIR / f".env.l1.{profile}"
    if profile and l2_env_file == (ROOT_DIR / ".env.l2.testnet"):
        l2_env_file = ROOT_DIR / f".env.l2.{profile}"
    return l1_env_file, l2_env_file


def _receiver_from_broadcast(rpc_url: str) -> str:
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


def _resolve_l2_receiver(l2: dict[str, str]) -> str:
    if l2.get("L2_RECEIVER"):
        return str(l2["L2_RECEIVER"])

    output_json = l2.get("OUTPUT_JSON")
    if output_json:
        out_path = Path(output_json)
        if not out_path.is_absolute():
            out_path = ROOT_DIR / out_path
        if out_path.is_file():
            data = json.loads(out_path.read_text(encoding="utf-8"))
            addrs = data.get("addrs", data)
            if addrs.get("l2Receiver"):
                return str(addrs["l2Receiver"])

    return _receiver_from_broadcast(must(l2, "RPC_URL"))


def _resolve_l2_wrapped_asset_from_tsa(l2_env_file: Path) -> str:
    l2 = load_env(l2_env_file)
    rpc_url = must(l2, "RPC_URL")

    receiver = _resolve_l2_receiver(l2)

    tsa = cast_call(rpc_url, receiver, "tsa()(address)")
    base = cast_call(
        rpc_url,
        tsa,
        "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
    )
    lines = [ln.strip() for ln in base.splitlines() if ln.strip()]
    if len(lines) < 3:
        raise ValueError("failed to parse getBaseTSAAddresses() output from TSA")
    return lines[2]


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Option(ROOT_DIR / ".env.l2.testnet", "--l2-env-file", help="L2 env file used for TSA lookup"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet. Loads .env.l1.<env> and .env.l2.<env>."),
    broadcast: bool = typer.Option(False, help="Execute onchain txs (default: dry-run/simulation)"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    require_cmd("forge")
    require_cmd("cast")

    l1_env_file, l2_env_file = _resolve_env_paths(env_profile, l1_env_file, l2_env_file)

    l1 = load_env(l1_env_file)

    for k in ("RPC_URL", "ACCOUNT", "TREASURY"):
        must(l1, k)

    # Keep keystore/named-account workflow only.
    deployer = run(["cast", "wallet", "address", "--account", l1["ACCOUNT"]])
    if not l1.get("ADMIN"):
        l1["ADMIN"] = deployer

    if not l1.get("OUTPUT_JSON"):
        chain_id = run(["cast", "chain-id", "--rpc-url", l1["RPC_URL"]])
        l1["OUTPUT_JSON"] = f"./deployments/{chain_id}/l1.json"

    out_abs = resolve_output_json(l1["OUTPUT_JSON"])
    out_abs.parent.mkdir(parents=True, exist_ok=True)

    if not l1.get("L2_EID") and l1.get("REMOTE_EID"):
        # Backward-compat fallback for older env files.
        l1["L2_EID"] = l1["REMOTE_EID"]

    env_overrides = {
        "ADMIN": l1["ADMIN"],
        "TREASURY": l1["TREASURY"],
        "OUTPUT_JSON": l1["OUTPUT_JSON"],
    }

    if l1.get("WETH_ASSET") and not l1.get("L2_WRAPPED_WETH_ASSET"):
        l1["L2_WRAPPED_WETH_ASSET"] = _resolve_l2_wrapped_asset_from_tsa(l2_env_file)
        print(
            "[cyan][info][/cyan] resolved L2_WRAPPED_WETH_ASSET from L2 TSA:",
            l1["L2_WRAPPED_WETH_ASSET"],
        )

    for opt in (
        "VAULT_OWNER",
        "PERMIT2",
        "L2_RECIPIENT",
        "LIQUIDITY_VAULT",
        "USDC_ASSET",
        "EULER_ADAPTER",
        "LZ_ENDPOINT",
        "L2_EID",
        "WETH_ASSET",
        "WETH_SOCKET_VAULT",
        "WETH_SOCKET_BRIDGE",
        "WETH_SOCKET_CONNECTOR",
        "WETH_MSG_GAS_LIMIT",
        "WETH_PAYLOAD_SIZE",
        "WETH_STRIKE_SCALE",
        "L2_WRAPPED_WETH_ASSET",
    ):
        if l1.get(opt):
            env_overrides[opt] = l1[opt]

    print(f"[cyan][info][/cyan] deploying L1 protocol via script/DeployL1.s.sol (broadcast={broadcast})")
    _forge_out = forge_script(
        "script/DeployL1.s.sol:DeployL1",
        l1["RPC_URL"],
        l1["ACCOUNT"],
        broadcast,
        env_overrides,
    )

    if json_out:
        print(
            json.dumps(
                {
                    "outputJson": str(out_abs),
                    "broadcast": broadcast,
                    "account": l1["ACCOUNT"],
                    "envProfile": (env_profile.strip().lower() or None),
                    "l1Env": str(l1_env_file),
                    "l2Env": str(l2_env_file),
                    "l2WrappedWethAsset": l1.get("L2_WRAPPED_WETH_ASSET"),
                },
                indent=2,
            )
        )
    else:
        print("[green][ok][/green] L1 deployment script finished")
        print(f"  output json: {out_abs}")
        print("  broadcast dir: script/../broadcast")


if __name__ == "__main__":
    app()
