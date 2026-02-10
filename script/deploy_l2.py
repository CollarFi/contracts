#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import ROOT_DIR, forge_script, load_env, must, require_cmd, resolve_output_json

app = typer.Typer(add_completion=False)


def _load_l1_addrs(path_value: str) -> tuple[str, str]:
    path = resolve_output_json(path_value)
    data = json.loads(path.read_text(encoding="utf-8"))
    addrs = data.get("addrs", data)
    l1_messenger = addrs.get("l1Messenger")
    l1_vault = addrs.get("l1Vault")
    if not l1_messenger or not l1_vault:
        raise ValueError(f"could not resolve l1Messenger/l1Vault from {path}")
    return str(l1_messenger), str(l1_vault)


@app.command()
def main(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    broadcast: bool = typer.Option(True, help="Execute onchain txs"),
    verify: bool = typer.Option(True, help="Verify contracts during deployment"),
    l1_output_json: str = typer.Option("", help="Optional L1 deployment JSON to auto-fill L1_MESSENGER/L1_VAULT"),
    verifier: str = typer.Option("", help="Optional forge verifier (e.g. blockscout, etherscan)") ,
    verifier_url: str = typer.Option("", help="Optional verifier URL"),
    etherscan_api_key: str = typer.Option("", help="Optional API key override for --etherscan-api-key"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    require_cmd("forge")
    require_cmd("cast")

    l2 = load_env(l2_env_file)
    for k in ("RPC_URL", "ACCOUNT", "OUTPUT_JSON", "ADMIN"):
        must(l2, k)

    # If creating a new TSA proxy, require init calldata so deploy+initialize happen atomically.
    if not l2.get("TSA_PROXY") and not l2.get("TSA_INIT_DATA"):
        raise ValueError(
            "TSA_INIT_DATA is required when TSA_PROXY is not provided (atomic deploy+init enforced)."
        )

    if (not l2.get("L1_MESSENGER") or not l2.get("L1_VAULT")) and l1_output_json:
        l1_messenger, l1_vault = _load_l1_addrs(l1_output_json)
        l2.setdefault("L1_MESSENGER", l1_messenger)
        l2.setdefault("L1_VAULT", l1_vault)

    for k in ("L1_MESSENGER", "L1_VAULT"):
        must(l2, k)

    out_abs = resolve_output_json(l2["OUTPUT_JSON"])
    out_abs.parent.mkdir(parents=True, exist_ok=True)

    extra_args: list[str] = []
    if verify:
        extra_args.append("--verify")
        if verifier:
            extra_args += ["--verifier", verifier]
        if verifier_url:
            extra_args += ["--verifier-url", verifier_url]
        api_key = etherscan_api_key or l2.get("ETHERSCAN_API_KEY", "")
        if api_key:
            extra_args += ["--etherscan-api-key", api_key]

    env_overrides = {
        "ADMIN": l2["ADMIN"],
        "L1_MESSENGER": l2["L1_MESSENGER"],
        "L1_VAULT": l2["L1_VAULT"],
        "OUTPUT_JSON": l2["OUTPUT_JSON"],
    }

    for opt in (
        "LZ_ENDPOINT",
        "SOCKET_TRACKER",
        "LOAN_STORE",
        "TSA_PROXY",
        "TSA_IMPLEMENTATION",
        "TSA_INIT_DATA",
        "L1_EID",
    ):
        if l2.get(opt):
            env_overrides[opt] = l2[opt]

    print(f"[cyan][info][/cyan] deploying L2 protocol via script/DeployL2.s.sol (broadcast={broadcast}, verify={verify})")
    forge_out = forge_script(
        "script/DeployL2.s.sol:DeployL2",
        l2["RPC_URL"],
        l2["ACCOUNT"],
        broadcast,
        env_overrides,
        extra_args=extra_args,
    )

    if json_out:
        print(
            json.dumps(
                {
                    "outputJson": str(out_abs),
                    "broadcast": broadcast,
                    "verify": verify,
                    "account": l2["ACCOUNT"],
                },
                indent=2,
            )
        )
    else:
        print("[green][ok][/green] L2 deployment script finished")
        print(f"  output json: {out_abs}")

    # Print where the broadcast artifacts and verification traces are.
    print("  broadcast dir: script/../broadcast")
    if verify:
        print("  verification: inspect forge output and broadcast logs for verification receipts")

    # Keep output available for debugging when run interactively.
    _ = forge_out


if __name__ == "__main__":
    app()
