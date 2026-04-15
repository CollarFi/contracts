#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, load_env, must, run  # noqa: E402
from management.handlers.l2_pending_message import (  # noqa: E402
    ensure_api_state,
    is_local_rpc,
    matching_addr,
    process_message_logs,
)
from management.handlers.l2_rfq_trade import (  # noqa: E402
    enqueue_rfq_trades_from_file,
    ensure_rfq_trade_state,
    process_rfq_trade_queue,
)
from management.handlers.l2_tsa_actions import ACTION_DEPOSIT_INTENT, ACTION_RETURN_REQUEST  # noqa: E402
from management.l2_common import assert_tsa_signer  # noqa: E402
from py_lib.deployments import resolve_addr  # noqa: E402
from py_lib.envs import resolve_l2_env_path  # noqa: E402
from py_lib.keeper_logs import LogRangeNotReadyError, get_message_received_logs  # noqa: E402
from py_lib.keeper_loop import resolve_scan_range, should_advance_cursor  # noqa: E402
from py_lib.keeper_signer import KeeperSigner  # noqa: E402
from py_lib.keeper_state import load_keeper_state, read_keeper_cursor, save_keeper_state, write_keeper_cursor  # noqa: E402

app = typer.Typer(add_completion=False)


@dataclass(frozen=True)
class L2KeeperRuntime:
    rpc_url: str
    receiver_addr: str
    tsa_addr: str
    matching_addr: str
    atomic_executor_addr: str
    deposit_module: str
    withdrawal_module: str
    rfq_module: str
    wrapped_deposit_asset: str
    state_file: Path
    max_per_tick: int
    broadcast: bool
    lz_fee_buffer_bps: int
    local_atomic_submit: bool
    submit_deposit_api: bool
    submit_withdraw_api: bool
    api_url: str
    derive_wallet: str
    derive_asset_name: str
    api_retry_attempts: int
    api_retry_initial_delay_seconds: float
    api_retry_max_delay_seconds: float
    allowed_actions: set[int]
    signer: KeeperSigner | None = None
    account: str = ""
    private_key: str = ""
    sender: str = ""
    unlocked: bool = False


def _read_addr_from_output(path_value: str, key: str) -> str:
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


def _extract_addresses(raw: str, expected: int, label: str) -> list[str]:
    addrs = re.findall(r"0x[a-fA-F0-9]{40}", raw)
    if len(addrs) < expected:
        raise RuntimeError(f"failed to parse {label}: {raw}")
    return addrs[:expected]


def _default_output_json(rpc_url: str, side: str) -> str:
    chain_id = run(["cast", "chain-id", "--rpc-url", rpc_url])
    return str(ROOT_DIR / "deployments" / chain_id / f"{side}.json")


def _resolve_atomic_executor_addr(env: dict[str, str], rpc_url: str) -> str:
    configured = (env.get("ATOMIC_EXECUTOR") or "").strip()
    if configured:
        return configured
    output_json = env.get("OUTPUT_JSON") or _default_output_json(rpc_url, "l2")
    try:
        return _read_addr_from_output(output_json, "l2AtomicExecutor")
    except Exception:
        return ""


def _resolve_local_atomic_config(
    rpc_url: str,
    tsa_addr: str,
    deposit_module: str,
    withdrawal_module: str,
    wrapped_deposit_asset: str,
) -> tuple[str, str, str]:
    resolved_deposit = deposit_module.strip()
    resolved_withdrawal = withdrawal_module.strip()
    resolved_wrapped = wrapped_deposit_asset.strip()

    if not (resolved_deposit and resolved_withdrawal):
        collar_addrs = _extract_addresses(
            cast_call(
                rpc_url,
                tsa_addr,
                "getCollarTSAAddresses()(address,address,address,address,address,address)",
            ),
            6,
            "getCollarTSAAddresses",
        )
        if not resolved_deposit:
            resolved_deposit = collar_addrs[1]
        if not resolved_withdrawal:
            resolved_withdrawal = collar_addrs[2]

    if not resolved_wrapped:
        base_addrs = _extract_addresses(
            cast_call(
                rpc_url,
                tsa_addr,
                "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
            ),
            7,
            "getBaseTSAAddresses",
        )
        resolved_wrapped = base_addrs[2]

    return resolved_deposit, resolved_withdrawal, resolved_wrapped


def _resolve_rfq_module_addr(rpc_url: str, tsa_addr: str, configured_rfq_module: str) -> str:
    if configured_rfq_module.strip():
        return configured_rfq_module.strip()

    collar_addrs = _extract_addresses(
        cast_call(
            rpc_url,
            tsa_addr,
            "getCollarTSAAddresses()(address,address,address,address,address,address)",
        ),
        6,
        "getCollarTSAAddresses",
    )
    return collar_addrs[4]


def _resolve_wrapped_deposit_asset_addr(rpc_url: str, tsa_addr: str, configured_wrapped_asset: str) -> str:
    if configured_wrapped_asset.strip():
        return configured_wrapped_asset.strip()

    base_addrs = _extract_addresses(
        cast_call(
            rpc_url,
            tsa_addr,
            "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
        ),
        7,
        "getBaseTSAAddresses",
    )
    return base_addrs[2]


def _block_number(rpc_url: str) -> int:
    return int(run(["cast", "block-number", "--rpc-url", rpc_url]))


def run_keeper_tick(
    runtime: L2KeeperRuntime,
    *,
    state: dict[str, Any],
    next_block: int,
    handled: list[dict[str, Any]],
    rfq_trade_file: Path | None,
) -> tuple[dict[str, Any], int]:
    ensure_api_state(state)
    ensure_rfq_trade_state(state)
    enqueue_summary = enqueue_rfq_trades_from_file(state, rfq_trade_file)
    if enqueue_summary["added"]:
        save_keeper_state(runtime.state_file, state)

    latest = _block_number(runtime.rpc_url)
    scan_range = resolve_scan_range(next_block, latest)
    if scan_range is None:
        return {
            "fromBlock": next_block,
            "toBlock": latest,
            "logs": 0,
            "attempted": 0,
            "sent": 0,
            "rfqTradeQueueAdded": enqueue_summary["added"],
            "rfqTradeQueueSkipped": enqueue_summary["skipped"],
        }, next_block

    scan_from, scan_to = scan_range
    attempts, sent, queue_blocked = process_rfq_trade_queue(runtime, state=state, handled=handled)
    logs: list[dict[str, Any]] = []

    if not queue_blocked and attempts < runtime.max_per_tick:
        try:
            logs = get_message_received_logs(runtime.rpc_url, runtime.receiver_addr, scan_from, scan_to)
        except LogRangeNotReadyError:
            return {
                "fromBlock": scan_from,
                "toBlock": scan_to,
                "logs": 0,
                "attempted": attempts,
                "sent": sent,
                "rfqTradeQueueAdded": enqueue_summary["added"],
                "rfqTradeQueueSkipped": enqueue_summary["skipped"],
                "headLag": True,
                "advancedCursor": False,
                "nextBlock": next_block,
            }, next_block
        log_attempts, log_sent = process_message_logs(
            runtime,
            state=state,
            logs=logs,
            handled=handled,
            attempts_so_far=attempts,
        )
        attempts += log_attempts
        sent += log_sent

    advanced = should_advance_cursor(
        broadcast=runtime.broadcast,
        attempts=attempts,
        sent=sent,
        advance_on_dry_run=False,
        blocked=queue_blocked,
    )
    if advanced:
        next_block = write_keeper_cursor(state, scan_to + 1)
        save_keeper_state(runtime.state_file, state)

    return {
        "fromBlock": scan_from,
        "toBlock": scan_to,
        "logs": len(logs),
        "attempted": attempts,
        "sent": sent,
        "rfqTradeQueueAdded": enqueue_summary["added"],
        "rfqTradeQueueSkipped": enqueue_summary["skipped"],
        "headLag": False,
        "advancedCursor": advanced,
        "nextBlock": next_block,
    }, next_block


@app.command()
def main(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    receiver: str = typer.Option("", "--receiver", help="Override L2 receiver address"),
    start_block: int = typer.Option(0, "--start-block", help="Start block when state file doesn't exist"),
    state_file: Path = typer.Option(ROOT_DIR / "deployments" / "keeper_l2_state.json", "--state-file"),
    poll_seconds: int = typer.Option(5, "--poll-seconds", min=1),
    once: bool = typer.Option(False, "--once", help="Run one polling tick and exit"),
    max_per_tick: int = typer.Option(10, "--max-per-tick", min=1),
    no_deposit_intents: bool = typer.Option(False, "--no-deposit-intents", help="Disable handling of DepositIntent messages."),
    no_return_requests: bool = typer.Option(False, "--no-return-requests", help="Disable handling of ReturnRequest messages."),
    broadcast: bool = typer.Option(False, help="Send onchain transactions (default: dry-run)"),
    private_key: str = typer.Option("", "--private-key", help="Use raw private key instead of --account"),
    from_addr: str = typer.Option("", "--from", help="Use unlocked sender address (for anvil --auto-impersonate)"),
    unlocked: bool = typer.Option(False, "--unlocked", help="Use unlocked mode with --from"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
    lz_fee_buffer_bps: int = typer.Option(500, "--lz-fee-buffer-bps", min=0, help="Buffer over quoted LZ native fee (bps)."),
    no_submit_deposit_api: bool = typer.Option(
        False,
        "--no-submit-deposit-api",
        help="Disable Derive private/deposit submission after handling DepositIntent.",
    ),
    no_submit_withdraw_api: bool = typer.Option(
        False,
        "--no-submit-withdraw-api",
        help="Disable Derive private/withdraw submission after handling ReturnRequest.",
    ),
    api_retry_attempts: int = typer.Option(
        6,
        "--api-retry-attempts",
        min=1,
        help="Retries for Derive API submit when signature propagation errors are returned.",
    ),
    api_retry_initial_delay_seconds: float = typer.Option(
        2.0,
        "--api-retry-initial-delay-seconds",
        min=0.0,
        help="Initial retry delay in seconds for Derive API retries.",
    ),
    api_retry_max_delay_seconds: float = typer.Option(
        20.0,
        "--api-retry-max-delay-seconds",
        min=0.0,
        help="Maximum retry delay in seconds for Derive API retries.",
    ),
    derive_api_url: str = typer.Option(
        "",
        "--derive-api-url",
        help="Derive API base URL (default: DERIVE_API_URL env or https://api-demo.lyra.finance)",
    ),
    derive_wallet: str = typer.Option(
        "",
        "--derive-wallet",
        help="X-LyraWallet header override (default: TSA address)",
    ),
    derive_asset_name: str = typer.Option(
        "",
        "--derive-asset-name",
        help="Asset name for private/public deposit payloads (default: DERIVE_ASSET_NAME env or ETH)",
    ),
    rfq_trade_file: Path | None = typer.Option(
        None,
        "--rfq-trade-file",
        help=(
            "Optional JSON file containing one RFQ post-fill trade confirmation or a list under "
            "`rfqTrades`. Entries are queued into keeper state and processed serially before scanned L2 messages."
        ),
    ),
) -> None:
    l2_env_file = resolve_l2_env_path(env_profile, l2_env_file)
    env = load_env(l2_env_file)

    rpc_url = must(env, "RPC_URL")
    account = env.get("ACCOUNT", "")
    pk = private_key or env.get("PRIVATE_KEY", "")
    sender = from_addr or env.get("FROM", "")
    use_unlocked = unlocked or (str(env.get("UNLOCKED", "")).lower() in {"1", "true", "yes"})
    receiver_addr = receiver or resolve_addr(env, "L2_RECEIVER", "l2Receiver", "l2")

    tsa_addr = cast_call(rpc_url, receiver_addr, "tsa()(address)").strip()
    matching_addr_value = matching_addr(rpc_url, tsa_addr, env.get("MATCHING", "").strip())
    atomic_executor_addr = _resolve_atomic_executor_addr(env, rpc_url)
    deposit_module = (env.get("DEPOSIT_MODULE") or "").strip()
    withdrawal_module = (env.get("WITHDRAWAL_MODULE") or "").strip()
    rfq_module = _resolve_rfq_module_addr(rpc_url, tsa_addr, (env.get("RFQ_MODULE") or "").strip())
    wrapped_deposit_asset = _resolve_wrapped_deposit_asset_addr(
        rpc_url,
        tsa_addr,
        (env.get("WRAPPED_DEPOSIT_ASSET") or "").strip(),
    )

    api_url = (derive_api_url or env.get("DERIVE_API_URL") or "https://api-demo.lyra.finance").strip()
    derive_asset_name = (derive_asset_name or env.get("DERIVE_ASSET_NAME") or "ETH").strip()
    derive_wallet_addr = (derive_wallet or env.get("DERIVE_WALLET") or tsa_addr).strip()
    local_atomic_submit = is_local_rpc(rpc_url)
    submit_deposit_api = broadcast and (not no_submit_deposit_api)
    submit_withdraw_api = broadcast and (not no_submit_withdraw_api)

    # Decide when we actually need a signer (txs or API signatures).
    # Local atomic submit only requires signing when we are broadcasting; in
    # dry-run mode we do not send any transactions or signatures.
    needs_signer = broadcast or submit_deposit_api or submit_withdraw_api

    keeper_signer: KeeperSigner | None = None
    if needs_signer:
        keeper_signer = KeeperSigner.from_env(
            rpc_url=rpc_url,
            account=account,
            private_key=pk,
            from_addr=sender,
            unlocked=use_unlocked,
        )
        if keeper_signer is None:
            raise ValueError(
                "missing auth: provide ACCOUNT, or --private-key, or --unlocked --from when "
                "broadcasting, using local atomic submit, or submitting via Derive API"
            )

    if (submit_deposit_api or submit_withdraw_api) and not local_atomic_submit:
        # Ensure the configured signer is a TSA signer for Derive API flows.
        assert_tsa_signer(rpc_url, tsa_addr, keeper_signer.address if keeper_signer else "")
    if broadcast and local_atomic_submit:
        deposit_module, withdrawal_module, wrapped_deposit_asset = _resolve_local_atomic_config(
            rpc_url,
            tsa_addr,
            deposit_module,
            withdrawal_module,
            wrapped_deposit_asset,
        )
        if not atomic_executor_addr:
            raise ValueError("local atomic submission requires ATOMIC_EXECUTOR in env")
        for key, value in (
            ("DEPOSIT_MODULE", deposit_module),
            ("WITHDRAWAL_MODULE", withdrawal_module),
            ("WRAPPED_DEPOSIT_ASSET", wrapped_deposit_asset),
        ):
            if not value:
                raise ValueError(f"local atomic submission requires {key} in env")

    state = load_keeper_state(state_file, start_block)
    ensure_api_state(state)
    ensure_rfq_trade_state(state)
    next_block = read_keeper_cursor(state, start_block)

    allowed_actions: set[int] = set()
    if not no_deposit_intents:
        allowed_actions.add(ACTION_DEPOSIT_INTENT)
    if not no_return_requests:
        allowed_actions.add(ACTION_RETURN_REQUEST)
    if not allowed_actions:
        raise ValueError("no actions enabled; use --deposit-intents and/or --return-requests")

    runtime = L2KeeperRuntime(
        rpc_url=rpc_url,
        receiver_addr=receiver_addr,
        tsa_addr=tsa_addr,
        matching_addr=matching_addr_value,
        atomic_executor_addr=atomic_executor_addr,
        deposit_module=deposit_module,
        withdrawal_module=withdrawal_module,
        rfq_module=rfq_module,
        wrapped_deposit_asset=wrapped_deposit_asset,
        state_file=state_file,
        max_per_tick=max_per_tick,
        broadcast=broadcast,
        lz_fee_buffer_bps=lz_fee_buffer_bps,
        local_atomic_submit=local_atomic_submit,
        submit_deposit_api=submit_deposit_api,
        submit_withdraw_api=submit_withdraw_api,
        api_url=api_url,
        derive_wallet=derive_wallet_addr,
        derive_asset_name=derive_asset_name,
        api_retry_attempts=api_retry_attempts,
        api_retry_initial_delay_seconds=api_retry_initial_delay_seconds,
        api_retry_max_delay_seconds=api_retry_max_delay_seconds,
        allowed_actions=allowed_actions,
        signer=keeper_signer,
        account=account,
        private_key=pk,
        sender=sender,
        unlocked=use_unlocked,
    )

    handled: list[dict[str, Any]] = []

    def tick() -> dict[str, Any]:
        nonlocal next_block
        result, next_block = run_keeper_tick(
            runtime,
            state=state,
            next_block=next_block,
            handled=handled,
            rfq_trade_file=rfq_trade_file,
        )
        return result

    if once:
        result = tick()
        out = {
            "mode": "broadcast" if broadcast else "dry-run",
            "receiver": receiver_addr,
            "stateFile": str(state_file),
            "tick": result,
            "handled": handled,
        }
        if json_out:
            print(json.dumps(out, indent=2))
        else:
            print(out)
        return

    print(
        f"[bold]L2 keeper loop[/bold] mode={'broadcast' if broadcast else 'dry-run'} "
        f"receiver={receiver_addr} poll={poll_seconds}s"
    )
    while True:
        try:
            result = tick()
            if result.get("headLag"):
                print(
                    f"[yellow][tick][/yellow] blocks {result['fromBlock']}..{result['toBlock']} "
                    f"waiting for rpc head sync; retrying"
                )
            elif result["attempted"]:
                print(
                    f"[cyan][tick][/cyan] blocks {result['fromBlock']}..{result['toBlock']} "
                    f"logs={result['logs']} attempted={result['attempted']} sent={result['sent']} "
                    f"advanced={result.get('advancedCursor', False)}"
                )
                for item in handled[-result["attempted"] :]:
                    tx_parts = []
                    if item.get("tx"):
                        tx_parts.append(f"handleTx={item['tx']}")
                    derive_api = item.get("deriveApi")
                    if isinstance(derive_api, dict) and derive_api.get("matchingTx"):
                        tx_parts.append(f"matchTx={derive_api['matchingTx']}")
                    if item.get("depositConfirmedTx"):
                        tx_parts.append(f"ackTx={item['depositConfirmedTx']}")
                    if item.get("collateralReturnedTx"):
                        tx_parts.append(f"returnTx={item['collateralReturnedTx']}")
                    if item.get("tradeConfirmedTx"):
                        tx_parts.append(f"tradeTx={item['tradeConfirmedTx']}")
                    tx_text = (" " + " ".join(tx_parts)) if tx_parts else ""
                    print(
                        f"  - {item['action']} loan={item['loanId']} guid={item.get('guid', item.get('queueKey', '?'))} "
                        f"block={item.get('eventBlock', '?')}{tx_text} -> {item['status']}"
                    )
        except Exception as exc:
            print(f"[red][error][/red] {exc}")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    app()
