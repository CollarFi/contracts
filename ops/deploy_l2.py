#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import ROOT_DIR, forge_script, load_env, must, require_cmd, resolve_output_json, run

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


def _load_l1_addrs(path_value: str) -> tuple[str, str]:
    path = resolve_output_json(path_value)
    data = json.loads(path.read_text(encoding="utf-8"))
    addrs = data.get("addrs", data)
    l1_messenger = addrs.get("l1Messenger")
    l1_vault = addrs.get("l1Vault")
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
    if not path.is_file():
        raise FileNotFoundError(f"matching registry file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    needed = ["matching", "deposit", "withdrawal", "trade", "rfq", "atomicExecutor"]
    missing = [k for k in needed if not data.get(k)]
    if missing:
        raise ValueError(f"registry missing keys {missing} in {path}")
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
    if not path.is_file():
        raise FileNotFoundError(f"core registry file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    needed = ["subAccounts", "auction", "cash", "srm"]
    missing = [k for k in needed if not data.get(k)]
    if missing:
        raise ValueError(f"core registry missing keys {missing} in {path}")
    return {
        "SUBACCOUNTS": str(data["subAccounts"]),
        "AUCTION": str(data["auction"]),
        "CASH": str(data["cash"]),
        "MANAGER": str(data["srm"]),
    }


def _load_market_registry(chain_id: str, market: str) -> dict[str, str]:
    path = ROOT_DIR / "lib" / "v2-matching" / "lib" / "v2-core" / "deployments" / chain_id / f"{market}.json"
    if not path.is_file():
        raise FileNotFoundError(f"market registry file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    needed = ["option", "spotFeed", "base"]
    missing = [k for k in needed if not data.get(k)]
    if missing:
        raise ValueError(f"market registry missing keys {missing} in {path}")
    return {
        "OPTION_ASSET": str(data["option"]),
        "BASE_FEED": str(data["spotFeed"]),
        # TSA expects wrappedDepositAsset = market base asset, not TSA token proxy.
        "WRAPPED_DEPOSIT_ASSET": str(data["base"]),
    }


@app.command()
def main(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet. Loads .env.l2.<env> (and .env.l1.<env> fallback)."),
    broadcast: bool = typer.Option(False, help="Execute onchain txs"),
    verify: bool = typer.Option(True, help="Verify contracts during deployment"),
    private_key: str = typer.Option("", "--private-key", help="Use raw private key instead of --account"),
    from_addr: str = typer.Option("", "--from", help="Use unlocked sender address (for anvil --auto-impersonate)"),
    unlocked: bool = typer.Option(False, "--unlocked", help="Use unlocked mode with --from"),
    proxy_admin_account: str = typer.Option("", "--proxy-admin-account", help="Signer used for proxy upgrades/deployments"),
    proxy_admin_private_key: str = typer.Option("", "--proxy-admin-private-key", help="Raw private key for proxy-admin signer"),
    proxy_admin_from: str = typer.Option("", "--proxy-admin-from", help="Unlocked sender for proxy-admin signer"),
    proxy_admin_unlocked: bool = typer.Option(False, "--proxy-admin-unlocked", help="Use unlocked mode with --proxy-admin-from"),
    l1_output_json: str = typer.Option("", help="Optional L1 deployment JSON to auto-fill L1_MESSENGER/L1_VAULT"),
    verifier: str = typer.Option("", help="Optional forge verifier (e.g. blockscout, etherscan)") ,
    verifier_url: str = typer.Option("", help="Optional verifier URL"),
    etherscan_api_key: str = typer.Option("", help="Optional API key override for --etherscan-api-key"),
    derive_registry_profile: str = typer.Option("", help="Registry profile: testnet|mainnet (or set in env DERIVE_REGISTRY_PROFILE)"),
    derive_registry_chain_id: str = typer.Option("", help="Explicit registry chain id override (e.g. 901, 957)"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    require_cmd("forge")
    require_cmd("cast")

    # If caller uses default l2 env path + --env profile, resolve to .env.l2.<env> automatically.
    if env_profile and l2_env_file == (ROOT_DIR / ".env.l2.testnet"):
        l2_env_file = ROOT_DIR / f".env.l2.{env_profile.strip().lower()}"

    l2 = load_env(l2_env_file)

    resolved_env = env_profile.strip().lower() or l2.get("ENV", "").strip().lower()
    l1_fallback: dict[str, str] = {}
    if resolved_env in {"testnet", "mainnet"}:
        l1_env_file = ROOT_DIR / f".env.l1.{resolved_env}"
        if l1_env_file.is_file():
            l1_fallback = load_env(l1_env_file)
        else:
            raise FileNotFoundError(f"expected L1 env file not found for --env {resolved_env}: {l1_env_file}")

    for k in ("RPC_URL", "SOCKET_TRACKER"):
        must(l2, k)

    account = l2.get("ACCOUNT", "")
    pk = private_key or l2.get("PRIVATE_KEY", "")
    sender = from_addr or l2.get("FROM", "")
    use_unlocked = unlocked or (str(l2.get("UNLOCKED", "")).lower() in {"1", "true", "yes"})

    if not _has_signer(account, pk, sender, use_unlocked):
        raise ValueError("provide ACCOUNT, or --private-key, or --unlocked --from")

    deployer = _resolve_signer_address(account, pk, sender, use_unlocked)

    pa_account = proxy_admin_account or l2.get("PROXY_ADMIN_ACCOUNT", "")
    pa_pk = proxy_admin_private_key or l2.get("PROXY_ADMIN_PRIVATE_KEY", "")
    pa_sender = proxy_admin_from or l2.get("PROXY_ADMIN_FROM", "")
    pa_unlocked = proxy_admin_unlocked or (str(l2.get("PROXY_ADMIN_UNLOCKED", "")).lower() in {"1", "true", "yes"})
    use_two_signers = _has_signer(pa_account, pa_pk, pa_sender, pa_unlocked)

    if not l2.get("ADMIN"):
        l2["ADMIN"] = deployer

    if not l2.get("OUTPUT_JSON"):
        chain_id = run(["cast", "chain-id", "--rpc-url", l2["RPC_URL"]])
        l2["OUTPUT_JSON"] = f"./deployments/{chain_id}/l2.json"

    profile = derive_registry_profile or l2.get("DERIVE_REGISTRY_PROFILE", "") or resolved_env
    chain_id_override = derive_registry_chain_id or l2.get("DERIVE_REGISTRY_CHAIN_ID", "")
    registry_resolved: dict[str, str] = {}
    registry_chain_id_used = ""
    if profile or chain_id_override:
        registry_chain_id_used = _resolve_registry_chain_id(profile or "testnet", chain_id_override)

        # Matching modules registry
        registry_resolved.update(_load_matching_registry(registry_chain_id_used))

        # Core registry (subaccounts/auction/cash/srm manager)
        registry_resolved.update(_load_core_registry(registry_chain_id_used))

        # Optional market registry for OPTION_ASSET + BASE_FEED
        market_name = (l2.get("DERIVE_MARKET") or "").strip()
        if market_name:
            registry_resolved.update(_load_market_registry(registry_chain_id_used, market_name))

        for k, v in registry_resolved.items():
            l2.setdefault(k, v)

    # If creating a new TSA proxy without explicit init calldata, ensure auto-init inputs are present.
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
        missing_auto = [k for k in auto_keys if not l2.get(k)]
        if missing_auto:
            raise ValueError(
                "auto TSA init encoding requires missing env vars: " + ", ".join(missing_auto)
            )

    if (not _has_nonzero_addr(l2.get("L1_MESSENGER", "")) or not _has_nonzero_addr(l2.get("L1_VAULT", ""))) and l1_output_json:
        l1_messenger, l1_vault = _load_l1_addrs(l1_output_json)
        l2.setdefault("L1_MESSENGER", l1_messenger)
        l2.setdefault("L1_VAULT", l1_vault)

    # Fallback: read L1 messenger/vault directly from .env.l1.<env>.
    if l1_fallback:
        l2.setdefault("L1_MESSENGER", l1_fallback.get("L1_MESSENGER", ""))
        l2.setdefault("L1_VAULT", l1_fallback.get("L1_VAULT", ""))

        # Or derive from L1 output JSON referenced by the L1 env.
    if (
        not _has_nonzero_addr(l2.get("L1_MESSENGER", "")) or not _has_nonzero_addr(l2.get("L1_VAULT", ""))
    ) and l1_fallback.get("OUTPUT_JSON"):
        l1_messenger, l1_vault = _load_l1_addrs(l1_fallback["OUTPUT_JSON"])
        l2.setdefault("L1_MESSENGER", l1_messenger)
        l2.setdefault("L1_VAULT", l1_vault)

    if _has_nonzero_addr(l2.get("TSA_PROXY", "")) and not _has_nonzero_addr(l2.get("LOAN_STORE", "")):
        l2["LOAN_STORE"] = _resolve_loan_store_from_tsa(l2["RPC_URL"], l2["TSA_PROXY"])
        print("[cyan][info][/cyan] resolved LOAN_STORE from TSA proxy:", l2["LOAN_STORE"])

    inferred_testnet = resolved_env == "testnet" or l2_env_file.name == ".env.l2.testnet"
    if not l2.get("L2_SOCKET_ADAPTER_MODE") and inferred_testnet:
        l2["L2_SOCKET_ADAPTER_MODE"] = "compat"

    out_abs = resolve_output_json(l2["OUTPUT_JSON"])
    out_abs.parent.mkdir(parents=True, exist_ok=True)

    extra_args: list[str] = []
    api_key = etherscan_api_key or l2.get("ETHERSCAN_API_KEY", "")
    has_verify_params = bool(verifier or verifier_url or api_key)
    verify_enabled = verify and has_verify_params

    if verify_enabled:
        extra_args.append("--verify")
        if verifier:
            extra_args += ["--verifier", verifier]
        if verifier_url:
            extra_args += ["--verifier-url", verifier_url]
        if api_key:
            extra_args += ["--etherscan-api-key", api_key]
    elif verify and not has_verify_params:
        print("[yellow][warn][/yellow] verify requested but verifier params not set; skipping forge --verify flags")

    env_overrides = {
        "ADMIN": l2["ADMIN"],
        "OUTPUT_JSON": l2["OUTPUT_JSON"],
    }
    if l2.get("PROXY_ADMIN"):
        env_overrides["PROXY_ADMIN"] = l2["PROXY_ADMIN"]

    for opt in (
        "L1_MESSENGER",
        "L1_VAULT",
        "LZ_ENDPOINT",
        "SOCKET_TRACKER",
        "LOAN_STORE",
        "TSA_PROXY",
        "L2_RECEIVER",
        "TSA_IMPLEMENTATION",
        "TSA_INIT_DATA",
        "L1_EID",
        "MATCHING",
        "ATOMIC_EXECUTOR",
        "DEPOSIT_MODULE",
        "WITHDRAWAL_MODULE",
        "TRADE_MODULE",
        "RFQ_MODULE",
        "SUBACCOUNTS",
        "AUCTION",
        "CASH",
        "WRAPPED_DEPOSIT_ASSET",
        "MANAGER",
        "BASE_FEED",
        "OPTION_ASSET",
        "OPTION_RISK_VERIFIER",
        "RFQ_VERIFIER",
        "WETH_ASSET",
        "WETH_SOCKET_BRIDGE",
        "WETH_SOCKET_CONNECTOR",
        "WETH_MSG_GAS_LIMIT",
        "WETH_PAYLOAD_SIZE",
        "L2_SOCKET_ADAPTER_MODE",
        "USDC_ASSET",
        "USDC_SOCKET_BRIDGE",
        "USDC_SOCKET_CONNECTOR",
        "USDC_MSG_GAS_LIMIT",
        "USDC_PAYLOAD_SIZE",
        "TSA_INITIAL_OWNER",
        "TSA_SYMBOL",
        "TSA_NAME",
        "TSA_MIN_SIGNATURE_EXPIRY",
        "TSA_MAX_SIGNATURE_EXPIRY",
        "TSA_OPTION_VOL_SLIPPAGE_FACTOR",
        "TSA_CALL_MAX_DELTA",
        "TSA_MAX_NEG_CASH",
        "TSA_OPTION_MIN_TIME_TO_EXPIRY",
        "TSA_OPTION_MAX_TIME_TO_EXPIRY",
        "TSA_PUT_MAX_PRICE_FACTOR",
        "TSA_WORST_SPOT_SELL_PRICE",
        "DISABLE_CODE_SIZE_LIMIT",
    ):
        if l2.get(opt):
            env_overrides[opt] = l2[opt]

    if registry_resolved:
        print(f"[cyan][info][/cyan] loaded derive module registry (chainId={registry_chain_id_used}):")
        for k, v in registry_resolved.items():
            print(f"  {k}: {v}")

    print(f"[cyan][info][/cyan] deploying L2 protocol via script/DeployL2.s.sol (broadcast={broadcast}, verify={verify_enabled})")
    if use_two_signers:
        phase1_overrides = dict(env_overrides)
        phase1_overrides["DEPLOY_PHASE"] = "proxy-admin"
        print("[cyan][info][/cyan] phase 1/2 (proxy-admin signer): proxy upgrades/deployments")
        forge_script(
            "script/DeployL2.s.sol:DeployL2",
            l2["RPC_URL"],
            pa_account or None,
            broadcast,
            phase1_overrides,
            extra_args=extra_args,
            private_key=pa_pk or None,
            from_addr=pa_sender or None,
            unlocked=pa_unlocked,
        )

        data = json.loads(out_abs.read_text(encoding="utf-8"))
        addrs = data.get("addrs", data)
        l2_tsa = addrs.get("l2Tsa")
        l2_receiver = addrs.get("l2Receiver")
        l2_loan_store = addrs.get("l2LoanStore")
        if not l2_tsa or not l2_receiver:
            raise ValueError("phase 1 output missing l2Tsa/l2Receiver")

        phase2_overrides = dict(env_overrides)
        phase2_overrides["DEPLOY_PHASE"] = "admin"
        phase2_overrides["TSA_PROXY"] = str(l2_tsa)
        phase2_overrides["L2_RECEIVER"] = str(l2_receiver)
        if l2_loan_store:
            phase2_overrides["LOAN_STORE"] = str(l2_loan_store)
        print("[cyan][info][/cyan] phase 2/2 (admin signer): protocol configuration")
        forge_out = forge_script(
            "script/DeployL2.s.sol:DeployL2",
            l2["RPC_URL"],
            account or None,
            broadcast,
            phase2_overrides,
            extra_args=extra_args,
            private_key=pk or None,
            from_addr=sender or None,
            unlocked=use_unlocked,
        )
    else:
        full_overrides = dict(env_overrides)
        full_overrides["DEPLOY_PHASE"] = "full"
        forge_out = forge_script(
            "script/DeployL2.s.sol:DeployL2",
            l2["RPC_URL"],
            account or None,
            broadcast,
            full_overrides,
            extra_args=extra_args,
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
                    "verify": verify_enabled,
                    "account": account or None,
                    "privateKey": bool(pk),
                    "from": sender or None,
                    "unlocked": use_unlocked,
                    "twoSigners": use_two_signers,
                    "proxyAdminAccount": pa_account or None,
                    "proxyAdminPrivateKey": bool(pa_pk),
                    "proxyAdminFrom": pa_sender or None,
                    "proxyAdminUnlocked": pa_unlocked,
                    "envProfile": resolved_env or None,
                    "registryProfile": profile or None,
                    "registryChainId": (registry_chain_id_used or None),
                    "registryResolved": registry_resolved,
                },
                indent=2,
            )
        )
    else:
        print("[green][ok][/green] L2 deployment script finished")
        print(f"  output json: {out_abs}")

    # Print where the broadcast artifacts and verification traces are.
    print("  broadcast dir: script/../broadcast")
    if verify_enabled:
        print("  verification: inspect forge output and broadcast logs for verification receipts")

    # Keep output available for debugging when run interactively.
    _ = forge_out


if __name__ == "__main__":
    app()
