#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import os

import typer
from rich import print

from lz_harness.common import load_env, must
from py_lib.deploy_engine import (
    DeployMode,
    VerificationConfig,
    build_l1_config,
    run_l1_deploy,
)
from py_lib.envs import resolve_l1_l2_env_paths
from py_lib.l2_discovery import (
    resolve_l2_receiver_from_env_file,
    resolve_l2_subaccount_id_from_tsa,
    resolve_l2_wrapped_asset_from_tsa,
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


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Option(
        ROOT_DIR / ".env.l2.testnet",
        "--l2-env-file",
        help="L2 env file used for TSA lookup",
    ),
    env_profile: str = typer.Option(
        "",
        "--env",
        help="Environment profile: testnet|mainnet. Loads .env.l1.<env> and .env.l2.<env>.",
    ),
    mode: str = typer.Option(
        "auto", "--mode", help="Deployment mode: auto|fresh|upgrade"
    ),
    broadcast: bool = typer.Option(
        False, help="Execute onchain txs (default: dry-run/simulation)"
    ),
    verify: bool = typer.Option(
        True, help="Verify deployed contracts after broadcast (can be disabled with --no-verify)"
    ),
    verifier: str = typer.Option(
        "", help="Optional forge verifier (e.g. blockscout, etherscan)"
    ),
    verifier_url: str = typer.Option("", help="Optional verifier URL"),
    etherscan_api_key: str = typer.Option(
        "", help="Optional API key override for verification"
    ),
    private_key: str = typer.Option(
        "", "--private-key", help="Use raw private key instead of --account"
    ),
    from_addr: str = typer.Option(
        "", "--from", help="Use unlocked sender address (for anvil --auto-impersonate)"
    ),
    unlocked: bool = typer.Option(
        False, "--unlocked", help="Use unlocked mode with --from"
    ),
    proxy_admin_account: str = typer.Option(
        "", "--proxy-admin-account", help="Signer used for proxy upgrades"
    ),
    proxy_admin_private_key: str = typer.Option(
        "", "--proxy-admin-private-key", help="Raw private key for proxy-admin signer"
    ),
    proxy_admin_from: str = typer.Option(
        "", "--proxy-admin-from", help="Unlocked sender for proxy-admin signer"
    ),
    proxy_admin_unlocked: bool = typer.Option(
        False,
        "--proxy-admin-unlocked",
        help="Use unlocked mode with --proxy-admin-from",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable summary"
    ),
) -> None:
    require_cmd("forge")
    require_cmd("cast")

    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(
        env_profile, l1_env_file, l2_env_file
    )

    l1 = load_env(l1_env_file)
    must(l1, "RPC_URL")
    must(l1, "TREASURY")

    deployer = resolve_signer(
        "deployer",
        SignerInput(
            account=l1.get("ACCOUNT", ""),
            private_key=private_key or l1.get("PRIVATE_KEY", ""),
            from_addr=from_addr or l1.get("FROM", ""),
            unlocked=unlocked
            or str(l1.get("UNLOCKED", "")).lower() in {"1", "true", "yes"},
            password_env_keys=("ACCOUNT_PASSWORD", "DEPLOYER_PASSWORD"),
        ),
    )
    if deployer is None:
        raise ValueError("provide ACCOUNT, or --private-key, or --unlocked --from")

    proxy_admin = resolve_signer(
        "proxy_admin",
        SignerInput(
            account=proxy_admin_account or l1.get("PROXY_ADMIN_ACCOUNT", ""),
            private_key=proxy_admin_private_key
            or l1.get("PROXY_ADMIN_PRIVATE_KEY", ""),
            from_addr=proxy_admin_from or l1.get("PROXY_ADMIN_FROM", ""),
            unlocked=proxy_admin_unlocked
            or str(l1.get("PROXY_ADMIN_UNLOCKED", "")).lower() in {"1", "true", "yes"},
            password_env_keys=(
                "PROXY_ADMIN_ACCOUNT_PASSWORD",
                "PROXY_ADMIN_PASSWORD",
                "ACCOUNT_PASSWORD",
            ),
        ),
    )

    if not l1.get("ADMIN"):
        l1["ADMIN"] = deployer.address
    if not l1.get("PROXY_ADMIN"):
        l1["PROXY_ADMIN"] = (
            proxy_admin.address if proxy_admin is not None else l1["ADMIN"]
        )
    if (
        proxy_admin is None
        and _has_nonzero_addr(l1.get("PROXY_ADMIN", ""))
        and l1["PROXY_ADMIN"].lower() == deployer.address.lower()
    ):
        proxy_admin = deployer

    if not l1.get("OUTPUT_JSON"):
        chain_id = run(["cast", "chain-id", "--rpc-url", l1["RPC_URL"]])
        l1["OUTPUT_JSON"] = f"./deployments/{chain_id}/l1.json"
    output_json = resolve_output_json(l1["OUTPUT_JSON"])
    output_json.parent.mkdir(parents=True, exist_ok=True)

    if not l1.get("L2_EID") and l1.get("REMOTE_EID"):
        l1["L2_EID"] = l1["REMOTE_EID"]

    if _has_nonzero_addr(l1.get("WETH_ASSET", "")) and not _has_nonzero_addr(
        l1.get("L2_WRAPPED_WETH_ASSET", "")
    ):
        l1["L2_WRAPPED_WETH_ASSET"] = resolve_l2_wrapped_asset_from_tsa(l2_env_file)
        print(
            "[cyan][info][/cyan] resolved L2_WRAPPED_WETH_ASSET from L2 TSA:",
            l1["L2_WRAPPED_WETH_ASSET"],
        )

    if not _has_nonzero_addr(l1.get("L2_RECIPIENT", "")):
        l1["L2_RECIPIENT"] = resolve_l2_receiver_from_env_file(l2_env_file)
        print(
            "[cyan][info][/cyan] resolved L2_RECIPIENT from L2 deployment/env:",
            l1["L2_RECIPIENT"],
        )

    if not l1.get("DERIVE_SUBACCOUNT_ID"):
        subaccount_id = resolve_l2_subaccount_id_from_tsa(l2_env_file)
        l1["DERIVE_SUBACCOUNT_ID"] = str(subaccount_id)
        print(
            "[cyan][info][/cyan] resolved DERIVE_SUBACCOUNT_ID from L2 TSA:",
            l1["DERIVE_SUBACCOUNT_ID"],
        )

    # Verification config comes from both CLI flags and env vars. CLI takes
    # precedence when provided, but env allows setting sensible defaults.
    verification = VerificationConfig(
        enabled=verify,
        verifier=verifier
        or l1.get("VERIFIER", "")
        or os.environ.get("VERIFIER", ""),
        verifier_url=verifier_url
        or l1.get("VERIFIER_URL", "")
        or os.environ.get("VERIFIER_URL", ""),
        etherscan_api_key=(
            etherscan_api_key
            or l1.get("ETHERSCAN_API_KEY", "")
            or os.environ.get("ETHERSCAN_API_KEY", "")
        ),
    )
    if verification.enabled and not (
        verification.verifier
        or verification.verifier_url
        or verification.etherscan_api_key
    ):
        print(
            "[yellow][warn][/yellow] verify requested without explicit verifier params; forge defaults will be used"
        )

    requested_mode = _resolve_mode(l1.get("MODE", mode))
    cfg = build_l1_config(l1, mode=requested_mode, proxy_admin_owner=l1["PROXY_ADMIN"])

    print(
        f"[cyan][info][/cyan] deploying L1 via Python engine "
        f"(broadcast={broadcast}, mode={requested_mode}, twoSigners={proxy_admin is not None})"
    )

    summary = run_l1_deploy(
        rpc_url=l1["RPC_URL"],
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
                    "meta": summary.meta,
                },
                indent=2,
            )
        )
        return

    print("[green][ok][/green] L1 deploy flow finished")
    print(f"  output json: {summary.output_json}")
    print(f"  mode: {summary.mode}")
    print(f"  executed steps: {len(summary.executed_steps)}")
    if not summary.broadcast:
        print("  dry-run only; no transactions were sent")


if __name__ == "__main__":
    app()
