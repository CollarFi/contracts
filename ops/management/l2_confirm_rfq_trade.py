#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, cast_send, load_env, must, run
from l2_common import extract_tx_hash
from py_lib.envs import resolve_l2_env_path

app = typer.Typer(add_completion=False)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + ("00" * 32)
ACTION_TRADE_CONFIRMED = 5


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


def _default_output_json(rpc_url: str, side: str) -> str:
    chain_value = run(["cast", "chain-id", "--rpc-url", rpc_url]).strip()
    return str(ROOT_DIR / "deployments" / chain_value / f"{side}.json")


def _resolve_receiver_addr(env: dict[str, str], rpc_url: str) -> str:
    if env.get("L2_RECEIVER"):
        return str(env["L2_RECEIVER"])
    output_json = env.get("OUTPUT_JSON") or _default_output_json(rpc_url, "l2")
    return _read_addr_from_output(output_json, "l2Receiver")


def _parse_uint(raw: str) -> int:
    return int(raw.strip().split()[0])


def _abi_encode(signature: str, *args: str) -> str:
    return run(["cast", "abi-encode", signature, *args]).strip()


def _quote_trade_confirm_native_fee(
    *,
    rpc_url: str,
    receiver_addr: str,
    asset: str,
    amount: int,
    socket_message_id: str,
    quote_hash: str,
    taker_nonce: int,
    call_strike: int,
    put_strike: int,
    expiry: int,
    loan_id: int,
    realized_c: int,
) -> int:
    tsa_addr = cast_call(rpc_url, receiver_addr, "tsa()(address)").strip()
    vault_recipient = cast_call(rpc_url, receiver_addr, "vaultRecipient()(address)").strip()
    subaccount_id = _parse_uint(cast_call(rpc_url, tsa_addr, "subAccount()(uint256)"))
    options = cast_call(rpc_url, receiver_addr, "defaultOptions()(bytes)")
    payload = _abi_encode(
        "f(uint256,uint256,uint64,int256)",
        str(call_strike),
        str(put_strike),
        str(expiry),
        str(realized_c),
    )
    message_tuple = (
        f"({ACTION_TRADE_CONFIRMED},"
        f"{loan_id},"
        f"{asset},"
        f"{amount},"
        f"{vault_recipient},"
        f"{subaccount_id},"
        f"{socket_message_id},"
        f"0,"
        f"{quote_hash},"
        f"{taker_nonce},"
        f"{payload})"
    )
    quote_raw = cast_call(
        rpc_url,
        receiver_addr,
        "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
        message_tuple,
        options,
    ).strip()
    if quote_raw.startswith("("):
        return _parse_uint(quote_raw.split(",", 1)[0].lstrip("(").strip())
    return _parse_uint(quote_raw)


@app.command()
def main(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    receiver: str = typer.Option("", "--receiver", help="Override L2 receiver address"),
    loan_id: int = typer.Option(..., "--loan-id", min=1),
    taker_nonce: int = typer.Option(..., "--taker-nonce", min=1),
    call_strike: int = typer.Option(..., "--call-strike", min=0),
    put_strike: int = typer.Option(..., "--put-strike", min=0),
    expiry: int = typer.Option(..., "--expiry", min=0),
    asset: str = typer.Option(ZERO_ADDRESS, "--asset"),
    amount: int = typer.Option(0, "--amount", min=0),
    socket_message_id: str = typer.Option(ZERO_BYTES32, "--socket-message-id"),
    quote_hash: str = typer.Option(ZERO_BYTES32, "--quote-hash"),
    realized_c: int = typer.Option(0, "--realized-c"),
    broadcast: bool = typer.Option(False, help="Send onchain transactions (default: dry-run)"),
    private_key: str = typer.Option("", "--private-key", help="Use raw private key instead of --account"),
    from_addr: str = typer.Option("", "--from", help="Use unlocked sender address (for anvil --auto-impersonate)"),
    unlocked: bool = typer.Option(False, "--unlocked", help="Use unlocked mode with --from"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
    lz_fee_buffer_bps: int = typer.Option(500, "--lz-fee-buffer-bps", min=0, help="Buffer over quoted LZ native fee (bps)."),
) -> None:
    l2_env_file = resolve_l2_env_path(env_profile, l2_env_file)
    env = load_env(l2_env_file)

    rpc_url = must(env, "RPC_URL")
    account = env.get("ACCOUNT", "")
    pk = private_key or env.get("PRIVATE_KEY", "")
    sender = from_addr or env.get("FROM", "")
    use_unlocked = unlocked or (str(env.get("UNLOCKED", "")).lower() in {"1", "true", "yes"})
    receiver_addr = receiver or _resolve_receiver_addr(env, rpc_url)
    if broadcast and not account and not pk and not (use_unlocked and sender):
        raise ValueError("missing auth for --broadcast: provide ACCOUNT, or --private-key, or --unlocked --from")

    quoted_fee = _quote_trade_confirm_native_fee(
        rpc_url=rpc_url,
        receiver_addr=receiver_addr,
        asset=asset,
        amount=amount,
        socket_message_id=socket_message_id,
        quote_hash=quote_hash,
        taker_nonce=taker_nonce,
        call_strike=call_strike,
        put_strike=put_strike,
        expiry=expiry,
        loan_id=loan_id,
        realized_c=realized_c,
    )
    fee_with_buffer = quoted_fee + (quoted_fee * lz_fee_buffer_bps) // 10_000

    out: dict[str, str] = {
        "mode": "broadcast" if broadcast else "dry-run",
        "receiver": receiver_addr,
        "loanId": str(loan_id),
        "takerNonce": str(taker_nonce),
        "quotedLzFee": str(quoted_fee),
        "quotedLzFeeWithBuffer": str(fee_with_buffer),
    }

    if broadcast:
        record_tx = cast_send(
            rpc_url,
            account or None,
            receiver_addr,
            "recordTradeExecuted(uint256,uint256)",
            str(loan_id),
            str(taker_nonce),
            private_key=pk or None,
            from_addr=sender or None,
            unlocked=use_unlocked,
        )
        confirm_tx = cast_send(
            rpc_url,
            account or None,
            receiver_addr,
            "sendTradeConfirmed((uint256,address,uint256,bytes32,bytes32,uint256,uint256,uint256,uint64,int256))",
            (
                f"({loan_id},{asset},{amount},{socket_message_id},{quote_hash},{taker_nonce},"
                f"{call_strike},{put_strike},{expiry},{realized_c})"
            ),
            value_wei=str(fee_with_buffer),
            private_key=pk or None,
            from_addr=sender or None,
            unlocked=use_unlocked,
        )
        out["recordTradeExecutedTx"] = extract_tx_hash(record_tx)
        out["sendTradeConfirmedTx"] = extract_tx_hash(confirm_tx)

    if json_out:
        print(json.dumps(out, indent=2))
    else:
        print(out)


if __name__ == "__main__":
    app()
