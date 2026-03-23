#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import load_env, must
from py_lib.deploy_engine import (
    DeployMode,
    VerificationConfig,
    build_l2_config,
    run_l2_deploy,
)
from py_lib.runtime import ROOT_DIR, require_cmd, resolve_output_json, run
from py_lib.signers import SignerInput, resolve_signer

app = typer.Typer(add_completion=False)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _has_nonzero_addr(value: str) -> bool:
    return bool(value) and value.lower() != ZERO_ADDRESS


def _resolve_mode(raw: str) -> DeployMode:
    mode = raw.strip().lower() or "auto"
    if mode not in {"auto", "fresh", "upgrade"}:
        raise ValueError("--mode must be one of: auto, fresh, upgrade")
    return mode  # type: ignore[return-value]


def _load_l1_addrs(path_value: str) -> tuple[str, str]:
    path = resolve_output_json(path_value)
    data = json.loads(path.read_text(encoding="utf-8"))
    l1_messenger = data.get("l1Messenger") or data.get("addrs", {}).get("l1Messenger")
    l1_vault = data.get("l1Vault") or data.get("addrs", {}).get("l1Vault")
    if not l1_messenger or not l1_vault:
        raise ValueError(f"could not resolve l1Messenger/l1Vault from {path}")
    return str(l1_messenger), str(l1_vault)


def _resolve_loan_store_from_tsa(rpc_url: str, tsa_proxy: str) -> str:
    out = run(["cast", "call", tsa_proxy, "loanStore()(address)", "--rpc-url", rpc_url]).strip()
    if not out:
        raise ValueError(f"could not resolve loanStore() from TSA proxy {tsa_proxy}")
    return out.split()[0]


def _resolve_registry_chain_id(profile: str, explicit_chain_id: str) -> str:
    if explicit_chain_id:
        return explicit_chain_id
    p = profile.strip().lower()
    if p in {"testnet", "derive-testnet", "lyra-testnet"}:
        return "901"
    if p in {"mainnet", "derive-mainnet", "lyra-mainnet"}:
        return "957"
    raise ValueError(
        "unknown DERIVE_REGISTRY_PROFILE; use one of: testnet/mainnet (or set DERIVE_REGISTRY_CHAIN_ID explicitly)"
    )


def _load_matching_registry(chain_id: str) -> dict[str, str]:
    path = ROOT_DIR / "lib" / "v2-matching" / "deployments" / chain_id / "matching.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "MATCHING": str(data["matching"]),
        "DEPOSIT_MODULE": str(data["deposit"]),
        "WITHDRAWAL_MODULE": str(data["withdrawal"]),
        "TRADE_MODULE": str(data["trade"]),
        "RFQ_MODULE": str(data["rfq"]),
        "ATOMIC_EXECUTOR": str(data["atomicExecutor"]),
    }


def _load_core_registry(chain_id: str) -> dict[str, str]:
    path = ROOT_DIR / "lib" / "v2-matching" / "lib" / "v2-core" / "deployments" / chain_id / "core.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "SUBACCOUNTS": str(data["subAccounts"]),
        "AUCTION": str(data["auction"]),
        "CASH": str(data["cash"]),
        "MANAGER": str(data["srm"]),
    }


def _load_market_registry(chain_id: str, market: str) -> dict[str, str]:
    path = ROOT_DIR / "lib" / "v2-matching" / "lib" / "v2-core" / "deployments" / chain_id / f"{market}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "OPTION_ASSET": str(data["option"]),
        "BASE_FEED": str(data["spotFeed"]),
        "WRAPPED_DEPOSIT_ASSET": str(data["base"]),
    }


@app.command()
def main(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet. Loads .env.l2.<env> (and .env.l1.<env> fallback)."),
    mode: str = typer.Option("auto", "--mode", help="Deployment mode: auto|fresh|upgrade"),
    broadcast: bool = typer.Option(False, help="Execute onchain txs"),
    verify: bool = typer.Option(True, help="Verify contracts after deployment"),
    private_key: str = typer.Option("", "--private-key", help="Use raw private key instead of --account"),
    from_addr: str = typer.Option("", "--from", help="Use unlocked sender address (for anvil --auto-impersonate)"),
    unlocked: bool = typer.Option(False, "--unlocked", help="Use unlocked mode with --from"),
    proxy_admin_account: str = typer.Option("", "--proxy-admin-account", help="Signer used for proxy upgrades"),
    proxy_admin_private_key: str = typer.Option("", "--proxy-admin-private-key", help="Raw private key for proxy-admin signer"),
    proxy_admin_from: str = typer.Option("", "--proxy-admin-from", help="Unlocked sender for proxy-admin signer"),
    proxy_admin_unlocked: bool = typer.Option(False, "--proxy-admin-unlocked", help="Use unlocked mode with --proxy-admin-from"),
    l1_output_json: str = typer.Option("", help="Optional L1 deployment JSON to auto-fill L1_MESSENGER/L1_VAULT"),
    verifier: str = typer.Option("", help="Optional forge verifier (e.g. blockscout, etherscan)"),
    verifier_url: str = typer.Option("", help="Optional verifier URL"),
    etherscan_api_key: str = typer.Option("", help="Optional API key override for verification"),
    derive_registry_profile: str = typer.Option("", help="Registry profile: testnet|mainnet"),
    derive_registry_chain_id: str = typer.Option("", help="Explicit registry chain id override (e.g. 901, 957)"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    require_cmd("forge")
    require_cmd("cast")

    if env_profile and l2_env_file == (ROOT_DIR / ".env.l2.testnet"):
        l2_env_file = ROOT_DIR / f".env.l2.{env_profile.strip().lower()}"

    l2 = load_env(l2_env_file)
    resolved_env = env_profile.strip().lower() or l2.get("ENV", "").strip().lower()

    l1_fallback: dict[str, str] = {}
    if resolved_env in {"testnet", "mainnet"}:
        l1_env_file = ROOT_DIR / f".env.l1.{resolved_env}"
        if l1_env_file.is_file():
            l1_fallback = load_env(l1_env_file)

    for key in ("RPC_URL", "SOCKET_TRACKER"):
        must(l2, key)

    deployer = resolve_signer(
        "deployer",
        SignerInput(
            account=l2.get("ACCOUNT", ""),
            private_key=private_key or l2.get("PRIVATE_KEY", ""),
            from_addr=from_addr or l2.get("FROM", ""),
            unlocked=unlocked or str(l2.get("UNLOCKED", "")).lower() in {"1", "true", "yes"},
            password_env_keys=("ACCOUNT_PASSWORD", "DEPLOYER_PASSWORD"),
        ),
    )
    if deployer is None:
        raise ValueError("provide ACCOUNT, or --private-key, or --unlocked --from")

    proxy_admin = resolve_signer(
        "proxy_admin",
        SignerInput(
            account=proxy_admin_account or l2.get("PROXY_ADMIN_ACCOUNT", ""),
            private_key=proxy_admin_private_key or l2.get("PROXY_ADMIN_PRIVATE_KEY", ""),
            from_addr=proxy_admin_from or l2.get("PROXY_ADMIN_FROM", ""),
            unlocked=proxy_admin_unlocked or str(l2.get("PROXY_ADMIN_UNLOCKED", "")).lower() in {"1", "true", "yes"},
            password_env_keys=("PROXY_ADMIN_ACCOUNT_PASSWORD", "PROXY_ADMIN_PASSWORD", "ACCOUNT_PASSWORD"),
        ),
    )

    if not l2.get("ADMIN"):
        l2["ADMIN"] = deployer.address
    if not l2.get("PROXY_ADMIN"):
        l2["PROXY_ADMIN"] = proxy_admin.address if proxy_admin is not None else l2["ADMIN"]
    if proxy_admin is None and _has_nonzero_addr(l2.get("PROXY_ADMIN", "")) and l2["PROXY_ADMIN"].lower() == deployer.address.lower():
        proxy_admin = deployer

    if not l2.get("OUTPUT_JSON"):
        chain_id = run(["cast", "chain-id", "--rpc-url", l2["RPC_URL"]])
        l2["OUTPUT_JSON"] = f"./deployments/{chain_id}/l2.json"
    output_json = resolve_output_json(l2["OUTPUT_JSON"])
    output_json.parent.mkdir(parents=True, exist_ok=True)

    profile = derive_registry_profile or l2.get("DERIVE_REGISTRY_PROFILE", "") or resolved_env
    chain_id_override = derive_registry_chain_id or l2.get("DERIVE_REGISTRY_CHAIN_ID", "")
    registry_resolved: dict[str, str] = {}
    registry_chain_id_used = ""
    if profile or chain_id_override:
        registry_chain_id_used = _resolve_registry_chain_id(profile or "testnet", chain_id_override)
        registry_resolved.update(_load_matching_registry(registry_chain_id_used))
        registry_resolved.update(_load_core_registry(registry_chain_id_used))
        market_name = (l2.get("DERIVE_MARKET") or "").strip()
        if market_name:
            registry_resolved.update(_load_market_registry(registry_chain_id_used, market_name))
        for key, value in registry_resolved.items():
            l2.setdefault(key, value)

    if not _has_nonzero_addr(l2.get("TSA_PROXY", "")) and not l2.get("TSA_INIT_DATA"):
        auto_keys = (
            "SUBACCOUNTS",
            "AUCTION",
            "CASH",
            "WRAPPED_DEPOSIT_ASSET",
            "MANAGER",
            "MATCHING",
            "BASE_FEED",
            "DEPOSIT_MODULE",
            "WITHDRAWAL_MODULE",
            "TRADE_MODULE",
            "RFQ_MODULE",
            "OPTION_ASSET",
        )
        missing = [key for key in auto_keys if not l2.get(key)]
        if missing:
            raise ValueError("auto TSA init encoding requires missing env vars: " + ", ".join(missing))

    if (not _has_nonzero_addr(l2.get("L1_MESSENGER", "")) or not _has_nonzero_addr(l2.get("L1_VAULT", ""))) and l1_output_json:
        l1_messenger, l1_vault = _load_l1_addrs(l1_output_json)
        l2.setdefault("L1_MESSENGER", l1_messenger)
        l2.setdefault("L1_VAULT", l1_vault)

    if l1_fallback:
        l2.setdefault("L1_MESSENGER", l1_fallback.get("L1_MESSENGER", ""))
        l2.setdefault("L1_VAULT", l1_fallback.get("L1_VAULT", ""))
        if (not _has_nonzero_addr(l2.get("L1_MESSENGER", "")) or not _has_nonzero_addr(l2.get("L1_VAULT", ""))) and l1_fallback.get("OUTPUT_JSON"):
            l1_messenger, l1_vault = _load_l1_addrs(l1_fallback["OUTPUT_JSON"])
            l2.setdefault("L1_MESSENGER", l1_messenger)
            l2.setdefault("L1_VAULT", l1_vault)

    if _has_nonzero_addr(l2.get("TSA_PROXY", "")) and not _has_nonzero_addr(l2.get("LOAN_STORE", "")):
        l2["LOAN_STORE"] = _resolve_loan_store_from_tsa(l2["RPC_URL"], l2["TSA_PROXY"])
        print("[cyan][info][/cyan] resolved LOAN_STORE from TSA proxy:", l2["LOAN_STORE"])

    inferred_testnet = resolved_env == "testnet" or l2_env_file.name == ".env.l2.testnet"
    if not l2.get("L2_SOCKET_ADAPTER_MODE") and inferred_testnet:
        l2["L2_SOCKET_ADAPTER_MODE"] = "compat"

    verification = VerificationConfig(
        enabled=verify,
        verifier=verifier,
        verifier_url=verifier_url,
        etherscan_api_key=etherscan_api_key or l2.get("ETHERSCAN_API_KEY", ""),
    )
    requested_mode = _resolve_mode(l2.get("MODE", mode))
    cfg = build_l2_config(l2, mode=requested_mode, proxy_admin_owner=l2["PROXY_ADMIN"])

    if registry_resolved:
        print(f"[cyan][info][/cyan] loaded derive registry (chainId={registry_chain_id_used}):")
        for key, value in registry_resolved.items():
            print(f"  {key}: {value}")

    print(
        f"[cyan][info][/cyan] deploying L2 via Python engine "
        f"(broadcast={broadcast}, mode={requested_mode}, verify={verification.enabled}, twoSigners={proxy_admin is not None})"
    )

    summary = run_l2_deploy(
        rpc_url=l2["RPC_URL"],
        output_json=output_json,
        cfg=cfg,
        signers={"deployer": deployer, "proxy_admin": proxy_admin},
        broadcast=broadcast,
        verification=verification,
    )

    if json_out:
        print(
            json.dumps(
                {
                    "outputJson": str(summary.output_json),
                    "broadcast": summary.broadcast,
                    "mode": summary.mode,
                    "signers": summary.meta.get("signers", {}),
                    "steps": summary.steps,
                    "executedSteps": summary.executed_steps,
                    "registryResolved": registry_resolved,
                    "meta": summary.meta,
                },
                indent=2,
            )
        )
        return

    print("[green][ok][/green] L2 deploy flow finished")
    print(f"  output json: {summary.output_json}")
    print(f"  mode: {summary.mode}")
    print(f"  executed steps: {len(summary.executed_steps)}")
    if not summary.broadcast:
        print("  dry-run only; no transactions were sent")


if __name__ == "__main__":
    app()
