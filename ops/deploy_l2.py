#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import ROOT_DIR, forge_script, load_env, must, require_cmd, resolve_output_json, run

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
    needed = ["matching", "deposit", "withdrawal", "trade", "rfq"]
    missing = [k for k in needed if not data.get(k)]
    if missing:
        raise ValueError(f"registry missing keys {missing} in {path}")
    return {
        "MATCHING": str(data["matching"]),
        "DEPOSIT_MODULE": str(data["deposit"]),
        "WITHDRAWAL_MODULE": str(data["withdrawal"]),
        "TRADE_MODULE": str(data["trade"]),
        "RFQ_MODULE": str(data["rfq"]),
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

    if not account and not pk and not (use_unlocked and sender):
        raise ValueError("provide ACCOUNT, or --private-key, or --unlocked --from")

    if not l2.get("ADMIN"):
        if pk:
            l2["ADMIN"] = run(["cast", "wallet", "address", "--private-key", pk])
        elif use_unlocked and sender:
            l2["ADMIN"] = sender
        else:
            l2["ADMIN"] = run(["cast", "wallet", "address", "--account", account])

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
    if not l2.get("TSA_PROXY") and not l2.get("TSA_INIT_DATA"):
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

    if (not l2.get("L1_MESSENGER") or not l2.get("L1_VAULT")) and l1_output_json:
        l1_messenger, l1_vault = _load_l1_addrs(l1_output_json)
        l2.setdefault("L1_MESSENGER", l1_messenger)
        l2.setdefault("L1_VAULT", l1_vault)

    # Fallback: read L1 messenger/vault directly from .env.l1.<env>.
    if l1_fallback:
        l2.setdefault("L1_MESSENGER", l1_fallback.get("L1_MESSENGER", ""))
        l2.setdefault("L1_VAULT", l1_fallback.get("L1_VAULT", ""))

        # Or derive from L1 output JSON referenced by the L1 env.
        if (not l2.get("L1_MESSENGER") or not l2.get("L1_VAULT")) and l1_fallback.get("OUTPUT_JSON"):
            l1_messenger, l1_vault = _load_l1_addrs(l1_fallback["OUTPUT_JSON"])
            l2.setdefault("L1_MESSENGER", l1_messenger)
            l2.setdefault("L1_VAULT", l1_vault)

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
        "TSA_IMPLEMENTATION",
        "TSA_INIT_DATA",
        "L1_EID",
        "MATCHING",
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
    ):
        if l2.get(opt):
            env_overrides[opt] = l2[opt]

    if registry_resolved:
        print(f"[cyan][info][/cyan] loaded derive module registry (chainId={registry_chain_id_used}):")
        for k, v in registry_resolved.items():
            print(f"  {k}: {v}")

    print(f"[cyan][info][/cyan] deploying L2 protocol via script/DeployL2.s.sol (broadcast={broadcast}, verify={verify_enabled})")
    forge_out = forge_script(
        "script/DeployL2.s.sol:DeployL2",
        l2["RPC_URL"],
        account or None,
        broadcast,
        env_overrides,
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
