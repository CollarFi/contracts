#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import ROOT_DIR, forge_script, load_env, must, require_cmd, resolve_output_json, run
from py_lib.envs import resolve_l1_l2_env_paths
from py_lib.l2_discovery import (
    resolve_l2_receiver_from_env_file,
    resolve_l2_subaccount_id_from_tsa,
    resolve_l2_wrapped_asset_from_tsa,
)

app = typer.Typer(add_completion=False)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _has_nonzero_addr(value: str) -> bool:
    return bool(value) and value.lower() != ZERO_ADDRESS


def _resolve_signer_address(account: str, private_key: str, from_addr: str, unlocked: bool) -> str:
    if private_key:
        return run(["cast", "wallet", "address", "--private-key", private_key])
    if unlocked and from_addr:
        return from_addr
    if account:
        return run(["cast", "wallet", "address", "--account", account])
    raise ValueError("invalid signer config")


def _has_signer(account: str, private_key: str, from_addr: str, unlocked: bool) -> bool:
    return bool(account or private_key or (unlocked and from_addr))


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Option(ROOT_DIR / ".env.l2.testnet", "--l2-env-file", help="L2 env file used for TSA lookup"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet. Loads .env.l1.<env> and .env.l2.<env>."),
    broadcast: bool = typer.Option(False, help="Execute onchain txs (default: dry-run/simulation)"),
    private_key: str = typer.Option("", "--private-key", help="Use raw private key instead of --account"),
    from_addr: str = typer.Option("", "--from", help="Use unlocked sender address (for anvil --auto-impersonate)"),
    unlocked: bool = typer.Option(False, "--unlocked", help="Use unlocked mode with --from"),
    proxy_admin_account: str = typer.Option("", "--proxy-admin-account", help="Signer used for proxy upgrades/deployments"),
    proxy_admin_private_key: str = typer.Option("", "--proxy-admin-private-key", help="Raw private key for proxy-admin signer"),
    proxy_admin_from: str = typer.Option("", "--proxy-admin-from", help="Unlocked sender for proxy-admin signer"),
    proxy_admin_unlocked: bool = typer.Option(False, "--proxy-admin-unlocked", help="Use unlocked mode with --proxy-admin-from"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    require_cmd("forge")
    require_cmd("cast")

    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)

    l1 = load_env(l1_env_file)

    must(l1, "RPC_URL")
    must(l1, "TREASURY")

    account = l1.get("ACCOUNT", "")
    pk = private_key or l1.get("PRIVATE_KEY", "")
    sender = from_addr or l1.get("FROM", "")
    use_unlocked = unlocked or (str(l1.get("UNLOCKED", "")).lower() in {"1", "true", "yes"})

    if not _has_signer(account, pk, sender, use_unlocked):
        raise ValueError("provide ACCOUNT, or --private-key, or --unlocked --from")

    deployer = _resolve_signer_address(account, pk, sender, use_unlocked)

    pa_account = proxy_admin_account or l1.get("PROXY_ADMIN_ACCOUNT", "")
    pa_pk = proxy_admin_private_key or l1.get("PROXY_ADMIN_PRIVATE_KEY", "")
    pa_sender = proxy_admin_from or l1.get("PROXY_ADMIN_FROM", "")
    pa_unlocked = proxy_admin_unlocked or (str(l1.get("PROXY_ADMIN_UNLOCKED", "")).lower() in {"1", "true", "yes"})
    use_two_signers = _has_signer(pa_account, pa_pk, pa_sender, pa_unlocked)
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
    if l1.get("PROXY_ADMIN"):
        env_overrides["PROXY_ADMIN"] = l1["PROXY_ADMIN"]

    if _has_nonzero_addr(l1.get("WETH_ASSET", "")) and not _has_nonzero_addr(l1.get("L2_WRAPPED_WETH_ASSET", "")):
        l1["L2_WRAPPED_WETH_ASSET"] = resolve_l2_wrapped_asset_from_tsa(l2_env_file)
        print(
            "[cyan][info][/cyan] resolved L2_WRAPPED_WETH_ASSET from L2 TSA:",
            l1["L2_WRAPPED_WETH_ASSET"],
        )

    if not _has_nonzero_addr(l1.get("L2_RECIPIENT", "")):
        l1["L2_RECIPIENT"] = resolve_l2_receiver_from_env_file(l2_env_file)
        print("[cyan][info][/cyan] resolved L2_RECIPIENT (receiver) from L2 deployment/env:", l1["L2_RECIPIENT"])

    if not l1.get("DERIVE_SUBACCOUNT_ID"):
        subaccount_id = resolve_l2_subaccount_id_from_tsa(l2_env_file)
        l1["DERIVE_SUBACCOUNT_ID"] = str(subaccount_id)
        print("[cyan][info][/cyan] resolved DERIVE_SUBACCOUNT_ID from L2 TSA:", l1["DERIVE_SUBACCOUNT_ID"])

    for opt in (
        "L1_VAULT",
        "L1_MESSENGER",
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
        "DERIVE_SUBACCOUNT_ID",
        "RFQ_SIGNER",
    ):
        if l1.get(opt):
            env_overrides[opt] = l1[opt]

    print(f"[cyan][info][/cyan] deploying L1 protocol via script/DeployL1.s.sol (broadcast={broadcast})")

    if use_two_signers:
        phase1_overrides = dict(env_overrides)
        phase1_overrides["DEPLOY_PHASE"] = "proxy-admin"
        print("[cyan][info][/cyan] phase 1/2 (proxy-admin signer): proxy upgrades/deployments")
        forge_script(
            "script/DeployL1.s.sol:DeployL1",
            l1["RPC_URL"],
            pa_account or None,
            broadcast,
            phase1_overrides,
            private_key=pa_pk or None,
            from_addr=pa_sender or None,
            unlocked=pa_unlocked,
        )

        data = json.loads(out_abs.read_text(encoding="utf-8"))
        addrs = data.get("addrs", data)
        l1_vault = addrs.get("l1Vault")
        l1_messenger = addrs.get("l1Messenger")
        if not l1_vault or not l1_messenger:
            raise ValueError("phase 1 output missing l1Vault/l1Messenger")

        phase2_overrides = dict(env_overrides)
        phase2_overrides["DEPLOY_PHASE"] = "admin"
        phase2_overrides["L1_VAULT"] = str(l1_vault)
        phase2_overrides["L1_MESSENGER"] = str(l1_messenger)
        print("[cyan][info][/cyan] phase 2/2 (admin signer): protocol configuration")
        _forge_out = forge_script(
            "script/DeployL1.s.sol:DeployL1",
            l1["RPC_URL"],
            account or None,
            broadcast,
            phase2_overrides,
            private_key=pk or None,
            from_addr=sender or None,
            unlocked=use_unlocked,
        )
    else:
        full_overrides = dict(env_overrides)
        full_overrides["DEPLOY_PHASE"] = "full"
        _forge_out = forge_script(
            "script/DeployL1.s.sol:DeployL1",
            l1["RPC_URL"],
            account or None,
            broadcast,
            full_overrides,
            private_key=pk or None,
            from_addr=sender or None,
            unlocked=use_unlocked,
        )

    if json_out:
        print(
            json.dumps(
                {
                    "outputJson": str(out_abs),
                    "broadcast": broadcast,
                    "account": account or None,
                    "privateKey": bool(pk),
                    "from": sender or None,
                    "unlocked": use_unlocked,
                    "twoSigners": use_two_signers,
                    "proxyAdminAccount": pa_account or None,
                    "proxyAdminPrivateKey": bool(pa_pk),
                    "proxyAdminFrom": pa_sender or None,
                    "proxyAdminUnlocked": pa_unlocked,
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
