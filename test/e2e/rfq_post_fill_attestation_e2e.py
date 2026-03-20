#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[2]
THIS_DIR = Path(__file__).resolve().parent

import sys

sys.path.insert(0, str(THIS_DIR))
from defaults import (  # noqa: E402
    L1_ANVIL_PORT,
    L1_ARTIFACT_JSON,
    L1_COLLATERAL_ASSET,
    L1_DEBT_ASSET,
    L1_WETH_SOCKET_CONNECTOR,
    L1_WETH_SOCKET_VAULT,
    L2_ANVIL_PORT,
    L2_ARTIFACT_JSON,
)
from common import (  # noqa: E402
    ANVIL_ADDR0,
    ANVIL_PK0,
    cast_call,
    cast_send_pk,
    ensure_l1_sepolia_rpc as _ensure_l1_sepolia_rpc,
    ensure_l2_derive_rpc as _ensure_l2_derive_rpc,
    ensure_live_deployments as _ensure_live_deployments,
    print_step as _print_step,
    relay_exact_lz_packet as _relay_exact_lz_packet,
    require_code as _require_code,
    resolve_l2_runtime_env as _resolve_l2_runtime_env,
    run,
    write_env_with_updates as _write_env_with_updates,
)
from loan_flow_helpers import (  # noqa: E402
    accept_mandate_for_pending,
    get_loan,
    get_mandate,
    get_pending,
    inject_deposit_confirmed,
    parse_json_or_fallback,
    run_fresh_atomic_pending_loan,
)

app = typer.Typer(add_completion=False)

BASE_MODULE_USED_NONCES_SLOT = 2
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + ("00" * 32)


def _run_keeper_command(cmd: list[str]) -> dict:
    env = dict(os.environ)
    env.update({"NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb"})
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"keeper command failed ({proc.returncode}): {proc.stderr.strip()}\n{proc.stdout}")
    return parse_json_or_fallback(proc.stdout)


def _to_bytes32(value: int) -> str:
    return f"0x{value:064x}"


def _keccak_hex(data_hex: str) -> str:
    return run(["cast", "keccak", data_hex]).splitlines()[0].strip()


def _mapping_slot_uint(key: int, slot: int) -> str:
    encoded = run(["cast", "abi-encode", "f(uint256,uint256)", str(key), str(slot)]).strip()
    return _keccak_hex(encoded)


def _mapping_slot_address(address_key: str, slot: int) -> int:
    encoded = run(["cast", "abi-encode", "f(address,uint256)", address_key, str(slot)]).strip()
    return int(_keccak_hex(encoded), 16)


def _set_storage(rpc: str, contract: str, slot: str, value: str) -> None:
    run(["cast", "rpc", "anvil_setStorageAt", contract, slot, value, "--rpc-url", rpc])


def _ensure_l2_keeper_role(l2_rpc: str, receiver: str) -> None:
    keeper_role = run(["cast", "keccak", "KEEPER_ROLE"]).strip()
    has = cast_call(l2_rpc, receiver, "hasRole(bytes32,address)(bool)", keeper_role, ANVIL_ADDR0).strip().lower() == "true"
    if has:
        return
    cast_send_pk(l2_rpc, receiver, "grantRole(bytes32,address)", keeper_role, ANVIL_ADDR0)


def _rfq_nonce_slot(l2_rpc: str, tsa: str, taker_nonce: int) -> tuple[str, str]:
    raw = cast_call(
        l2_rpc,
        tsa,
        "getCollarTSAAddresses()(address,address,address,address,address,address)",
    )
    addrs = re.findall(r"0x[a-fA-F0-9]{40}", raw)
    if len(addrs) < 5:
        raise RuntimeError(f"failed to parse TSA collar addrs: {raw}")
    rfq_module = addrs[4]
    owner_slot = _mapping_slot_address(tsa, BASE_MODULE_USED_NONCES_SLOT)
    module_slot = _mapping_slot_uint(taker_nonce, owner_slot)
    return rfq_module, module_slot


def _set_rfq_nonce_used(l2_rpc: str, tsa: str, taker_nonce: int) -> tuple[str, str]:
    rfq_module, module_slot = _rfq_nonce_slot(l2_rpc, tsa, taker_nonce)
    _set_storage(l2_rpc, rfq_module, module_slot, _to_bytes32(1))
    return rfq_module, module_slot


def _normalize_decimal_str(value: str | int) -> str:
    dec = Decimal(str(value).strip())
    rendered = format(dec.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _decimal_1e18_to_int(value: str | int) -> int:
    return int(Decimal(str(value).strip()) * (Decimal(10) ** 18))


def _int_1e18_to_decimal_str(value: int) -> str:
    return _normalize_decimal_str(Decimal(value) / (Decimal(10) ** 18))


def _option_sub_id(expiry: int, strike: int, is_call: bool) -> int:
    if strike % (10**10) != 0:
        raise RuntimeError(f"strike is too granular for option subId: {strike}")
    strike_8_decimals = strike // (10**10)
    return expiry | (strike_8_decimals << 32) | ((1 if is_call else 0) << 95)


def _inverse_direction(direction: str) -> str:
    normalized = direction.strip().lower()
    if normalized == "buy":
        return "sell"
    if normalized == "sell":
        return "buy"
    raise RuntimeError(f"invalid RFQ direction: {direction}")


def _signed_amount(direction: str, global_direction: str, amount: str) -> int:
    leg_sign = 1 if direction.strip().lower() == "buy" else -1
    quote_sign = 1 if global_direction.strip().lower() == "buy" else -1
    return _decimal_1e18_to_int(amount) * leg_sign * quote_sign


def _trade_array_literal(legs: list[dict], global_direction: str) -> str:
    tuples = []
    for leg in legs:
        tuples.append(
            (
                f"({leg['asset_address']},{int(leg['sub_id'])},{_decimal_1e18_to_int(leg['price'])},"
                f"{_signed_amount(str(leg['direction']), global_direction, str(leg['amount']))})"
            )
        )
    return "[" + ",".join(tuples) + "]"


def _rfq_sign_payloads(loan_id: int, execute_direction: str, legs: list[dict], max_fee: str) -> tuple[str, str]:
    maker_trades_data = run(
        [
            "cast",
            "abi-encode",
            "f((address,uint256,uint256,int256)[])",
            _trade_array_literal(legs, _inverse_direction(execute_direction)),
        ]
    ).strip()
    order_hash = run(["cast", "keccak", maker_trades_data]).splitlines()[0].strip()
    extra_data = run(["cast", "abi-encode", "f(uint256,bytes)", str(loan_id), maker_trades_data]).strip()
    action_data = run(
        ["cast", "abi-encode", "f(bytes32,uint256)", order_hash, str(_decimal_1e18_to_int(max_fee))]
    ).strip()
    return extra_data, action_data


def _action_tuple(subaccount_id: int, nonce: int, module: str, data: str, expiry: int, owner: str, signer: str) -> str:
    return f"({subaccount_id},{nonce},{module},{data},{expiry},{owner},{signer})"


def _ensure_receiver_is_submitter(l2_rpc: str, tsa: str, receiver: str) -> None:
    is_submitter = cast_call(l2_rpc, tsa, "isSubmitter(address)(bool)", receiver).strip().lower() == "true"
    if not is_submitter:
        raise RuntimeError(f"receiver is not configured as TSA submitter: {receiver}")


def _view_call_ok(rpc: str, to: str, sig: str, arg: str) -> bool:
    try:
        cast_call(rpc, to, sig, arg)
        return True
    except Exception:
        return False


def _tsa_params(l2_rpc: str, tsa: str) -> list[int]:
    raw = cast_call(
        l2_rpc,
        tsa,
        "getCollarTSAParams()((uint256,uint256,uint256,uint256,int256,uint256,uint256,uint256))",
    )
    parts = [int(token) for token in re.findall(r"-?\d+", re.sub(r"\s*\[[^\]]+\]", "", raw))]
    if len(parts) < 8:
        raise RuntimeError(f"failed to parse TSA params: {raw}")
    return parts[:8]


def _supported_option_expiries(l2_rpc: str, tsa: str, option_asset: str) -> list[int]:
    base_tsa_addrs = re.findall(
        r"0x[a-fA-F0-9]{40}",
        cast_call(l2_rpc, tsa, "getBaseTSAAddresses()(address,address,address,address,address,address,address)"),
    )
    if len(base_tsa_addrs) < 6:
        raise RuntimeError("failed to parse BaseTSA addresses for supported expiry lookup")
    manager = base_tsa_addrs[5]

    market_detail_raw = cast_call(l2_rpc, manager, "assetDetails(address)((bool,uint8,uint256))", option_asset)
    market_detail = [token for token in re.findall(r"\w+", market_detail_raw) if token.lower() not in {"true", "false"}]
    if len(market_detail) < 2:
        raise RuntimeError(f"failed to parse manager assetDetails: {market_detail_raw}")
    market_id = int(market_detail[1])

    market_feeds_raw = cast_call(l2_rpc, manager, "getMarketFeeds(uint256)(address,address,address)", str(market_id))
    market_feeds = re.findall(r"0x[a-fA-F0-9]{40}", market_feeds_raw)
    if len(market_feeds) < 3:
        raise RuntimeError(f"failed to parse manager feeds: {market_feeds_raw}")
    vol_feed = market_feeds[2]

    latest_block = int(run(["cast", "block-number", "--rpc-url", l2_rpc]))
    from_block = max(0, latest_block - 20_000)
    logs = json.loads(
        run(
            [
                "cast",
                "logs",
                "VolDataUpdated(uint64,(int256,uint256,int256,int256,uint256,uint256,uint64,uint64,uint64))",
                "--address",
                vol_feed,
                "--from-block",
                str(from_block),
                "--to-block",
                str(latest_block),
                "--rpc-url",
                l2_rpc,
                "--json",
            ]
        )
    )
    return sorted({int(log["topics"][1], 16) for log in logs if isinstance(log, dict) and isinstance(log.get("topics"), list)})


def _pick_rfq_maturity(l2_rpc: str, tsa: str, option_asset: str) -> int:
    params = _tsa_params(l2_rpc, tsa)
    option_min_time_to_expiry = int(params[5])
    now = int(json.loads(run(["cast", "block", "latest", "--rpc-url", l2_rpc, "--json"]))["timestamp"], 0)
    expiries = _supported_option_expiries(l2_rpc, tsa, option_asset)
    preferred_floor = now + max(option_min_time_to_expiry, 6 * 24 * 3600)
    fallback_floor = now + option_min_time_to_expiry

    for expiry in expiries:
        if expiry >= preferred_floor:
            return expiry
    for expiry in expiries:
        if expiry >= fallback_floor:
            return expiry
    raise RuntimeError(f"failed to find supported option expiry after {fallback_floor}")


def _find_valid_option_prices(
    *,
    l2_rpc: str,
    tsa: str,
    option_risk_verifier: str,
    option_asset: str,
    manager: str,
    expiry: int,
    call_strike: int,
    put_strike: int,
) -> tuple[str, str]:
    (
        _min_sig_expiry,
        _max_sig_expiry,
        option_vol_slippage_factor,
        call_max_delta,
        _max_neg_cash,
        option_min_time_to_expiry,
        option_max_time_to_expiry,
        put_max_price_factor,
    ) = _tsa_params(l2_rpc, tsa)

    def _call_ok(limit_price: int) -> bool:
        return _view_call_ok(
            l2_rpc,
            option_risk_verifier,
            "validateCall((address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint256))",
            (
                f"({manager},{option_asset},{expiry},{call_strike},{limit_price},{option_vol_slippage_factor},"
                f"{call_max_delta},{option_min_time_to_expiry},{option_max_time_to_expiry})"
            ),
        )

    def _put_ok(limit_price: int) -> bool:
        return _view_call_ok(
            l2_rpc,
            option_risk_verifier,
            "validatePut((address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint256))",
            (
                f"({manager},{option_asset},{expiry},{put_strike},{limit_price},{option_vol_slippage_factor},"
                f"{put_max_price_factor},{option_min_time_to_expiry},{option_max_time_to_expiry})"
            ),
        )

    max_search_price = 10**24

    call_hi = 1
    while call_hi <= max_search_price and not _call_ok(call_hi):
        call_hi *= 2
    if call_hi > max_search_price:
        raise RuntimeError("failed to find a call price accepted by OptionRiskVerifier")

    call_lo = 0
    while call_lo + 1 < call_hi:
        mid = (call_lo + call_hi) // 2
        if _call_ok(mid):
            call_hi = mid
        else:
            call_lo = mid
    min_call_price = call_hi

    put_lo = 0
    put_hi = 1
    while put_hi <= max_search_price and _put_ok(put_hi):
        put_lo = put_hi
        put_hi *= 2
    if put_lo == 0 and not _put_ok(put_lo):
        raise RuntimeError("failed to find a put price accepted by OptionRiskVerifier")
    if put_hi > max_search_price and _put_ok(put_lo):
        max_put_price = put_lo
    else:
        while put_lo + 1 < put_hi:
            mid = (put_lo + put_hi) // 2
            if _put_ok(mid):
                put_lo = mid
            else:
                put_hi = mid
        max_put_price = put_lo

    if min_call_price > max_put_price:
        raise RuntimeError(
            "no RFQ price overlap satisfies both option risk bounds "
            f"(min_call={_int_1e18_to_decimal_str(min_call_price)}, "
            f"max_put={_int_1e18_to_decimal_str(max_put_price)})"
        )

    shared_price = min_call_price
    return _int_1e18_to_decimal_str(shared_price), _int_1e18_to_decimal_str(shared_price)


def _call_strike_is_risk_valid(
    *,
    l2_rpc: str,
    tsa: str,
    option_risk_verifier: str,
    option_asset: str,
    manager: str,
    expiry: int,
    call_strike: int,
) -> bool:
    (
        _min_sig_expiry,
        _max_sig_expiry,
        option_vol_slippage_factor,
        call_max_delta,
        _max_neg_cash,
        option_min_time_to_expiry,
        option_max_time_to_expiry,
        _put_max_price_factor,
    ) = _tsa_params(l2_rpc, tsa)

    return _view_call_ok(
        l2_rpc,
        option_risk_verifier,
        "validateCall((address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint256))",
        (
            f"({manager},{option_asset},{expiry},{call_strike},{10**24},{option_vol_slippage_factor},"
            f"{call_max_delta},{option_min_time_to_expiry},{option_max_time_to_expiry})"
        ),
    )


def _shared_price_candidates() -> list[str]:
    return ["0.01", "0.05", "0.1", "0.2", "0.5", "1", "2", "5", "10", "20", "50"]


def _call_strike_candidates(min_call_strike: int) -> list[int]:
    step = 10**18
    offsets = [
        0,
        1,
        5,
        10,
        25,
        50,
        75,
        100,
        125,
        150,
        200,
        250,
        300,
        400,
        500,
        750,
        1000,
        1100,
        1250,
        1500,
        2000,
        3000,
        5000,
    ]
    candidates: list[int] = []
    for offset in offsets:
        strike = min_call_strike + (offset * step)
        if strike not in candidates:
            candidates.append(strike)
    return candidates


def _find_valid_option_terms(
    *,
    l2_rpc: str,
    tsa: str,
    option_risk_verifier: str,
    option_asset: str,
    manager: str,
    expiry: int,
    min_call_strike: int,
    max_put_strike: int,
) -> tuple[int, int, str, str]:
    (
        _min_sig_expiry,
        _max_sig_expiry,
        option_vol_slippage_factor,
        call_max_delta,
        _max_neg_cash,
        option_min_time_to_expiry,
        option_max_time_to_expiry,
        put_max_price_factor,
    ) = _tsa_params(l2_rpc, tsa)

    for candidate_call_strike in _call_strike_candidates(min_call_strike):
        if not _call_strike_is_risk_valid(
            l2_rpc=l2_rpc,
            tsa=tsa,
            option_risk_verifier=option_risk_verifier,
            option_asset=option_asset,
            manager=manager,
            expiry=expiry,
            call_strike=candidate_call_strike,
        ):
            continue
        for price in _shared_price_candidates():
            limit_price = str(_decimal_1e18_to_int(price))
            call_ok = _view_call_ok(
                l2_rpc,
                option_risk_verifier,
                "validateCall((address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint256))",
                (
                    f"({manager},{option_asset},{expiry},{candidate_call_strike},{limit_price},{option_vol_slippage_factor},"
                    f"{call_max_delta},{option_min_time_to_expiry},{option_max_time_to_expiry})"
                ),
            )
            if not call_ok:
                continue
            put_ok = _view_call_ok(
                l2_rpc,
                option_risk_verifier,
                "validatePut((address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint256))",
                (
                    f"({manager},{option_asset},{expiry},{max_put_strike},{limit_price},{option_vol_slippage_factor},"
                    f"{put_max_price_factor},{option_min_time_to_expiry},{option_max_time_to_expiry})"
                ),
            )
            if put_ok:
                normalized_price = _normalize_decimal_str(price)
                return candidate_call_strike, max_put_strike, normalized_price, normalized_price

    raise RuntimeError(
        "failed to find RFQ option terms inside mandate bounds "
        f"(min_call_strike={min_call_strike}, max_put_strike={max_put_strike})"
    )


def _start_mock_derive_api(l2_rpc: str, tsa: str, taker_nonce: int, expected_request: dict) -> tuple[HTTPServer, str, dict]:
    _, module_slot = _rfq_nonce_slot(l2_rpc, tsa, taker_nonce)
    state: dict[str, object] = {"requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception as exc:  # pragma: no cover - defensive
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(exc).encode("utf-8"))
                return

            state["requests"].append({"path": self.path, "headers": dict(self.headers.items()), "body": body})

            if self.path != "/private/execute_quote":
                self.send_response(404)
                self.end_headers()
                return

            for key, expected in expected_request.items():
                if key == "rfq_module":
                    continue
                actual = body.get(key)
                if actual != expected:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(
                        json.dumps({"error": f"unexpected {key}: expected {expected!r}, got {actual!r}"}).encode(
                            "utf-8"
                        )
                    )
                    return

            if not self.headers.get("X-LyraWallet"):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"missing X-LyraWallet"}')
                return
            if not body.get("signature"):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"missing signature"}')
                return

            _set_storage(l2_rpc, expected_request["rfq_module"], module_slot, _to_bytes32(1))

            response = {
                "id": "mock-execute-quote",
                "result": {
                    "status": "filled",
                    "quote_id": body["quote_id"],
                    "rfq_id": body["rfq_id"],
                },
            }
            payload = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    setattr(server, "_thread", thread)
    return server, f"http://127.0.0.1:{server.server_port}", state


def _relay_and_handle_mandate_created(l1_rpc: str, l2_rpc: str, receiver: str, origination_tx: str) -> dict:
    relayed = _relay_exact_lz_packet(l1_rpc, l2_rpc, origination_tx, occurrence=1)
    cast_send_pk(l2_rpc, receiver, "handleMessage(bytes32)", relayed["guid"])
    return relayed


@app.command()
def main(
    l1_json: Path = typer.Option(L1_ARTIFACT_JSON),
    l2_json: Path = typer.Option(L2_ARTIFACT_JSON),
    l1_rpc: str = typer.Option(f"http://127.0.0.1:{L1_ANVIL_PORT}"),
    l2_rpc: str = typer.Option(f"http://127.0.0.1:{L2_ANVIL_PORT}"),
    sepolia_usdc: str = typer.Option(L1_DEBT_ASSET),
    sepolia_weth: str = typer.Option(L1_COLLATERAL_ASSET),
    auto_redeploy: bool = typer.Option(True, "--auto-redeploy/--no-auto-redeploy"),
):
    print("=== collar.fi rfq post-fill attestation e2e ===")

    _ensure_l1_sepolia_rpc(l1_rpc)
    _ensure_l2_derive_rpc(l2_rpc)
    _print_step(True, "RPC topology verified (L1=11155111, L2=901)")

    _require_code(l1_rpc, sepolia_weth, "Sepolia WETH collateral")
    _require_code(l1_rpc, sepolia_usdc, "Sepolia USDC debt")

    l1_path = ROOT / l1_json
    l2_path = ROOT / l2_json
    l1, l2, redeployed = _ensure_live_deployments(
        l1_path,
        l2_path,
        l1_rpc,
        l2_rpc,
        sepolia_usdc,
        sepolia_weth,
        auto_redeploy,
        L1_WETH_SOCKET_VAULT,
        L1_WETH_SOCKET_CONNECTOR,
    )
    if redeployed:
        _print_step(True, "Detected stale deployments/runtime and refreshed via deployment_e2e")

    vault = l1["l1Vault"]
    messenger = l1["l1Messenger"]
    receiver = l2["l2Receiver"]
    tsa = l2["l2Tsa"]
    _ensure_l2_keeper_role(l2_rpc, receiver)

    initial_runtime = _resolve_l2_runtime_env(l2_rpc, l2, receiver)
    initial_option_asset = initial_runtime["OPTION_ASSET"]
    rfq_maturity = _pick_rfq_maturity(l2_rpc, l2["l2Tsa"], initial_option_asset)

    fresh = run_fresh_atomic_pending_loan(l1_json, l2_json, l1_rpc, l2_rpc, sepolia_weth, maturity=rfq_maturity)
    loan_id = int(fresh["loanId"])
    deposit_guid = fresh["depositGuid"]
    create_deposit = next(
        (step.get("result") for step in fresh["flow"].get("steps", []) if step.get("step") == "create_deposit_with_permit"),
        None,
    )
    if not isinstance(create_deposit, dict) or not create_deposit.get("tx"):
        raise RuntimeError("fresh loan flow missing create_deposit_with_permit tx for mandate relay")
    pending = get_pending(vault, l1_rpc, loan_id)
    mandate = get_mandate(vault, l1_rpc, loan_id)
    if mandate["borrower"] == ZERO_ADDRESS:
        mandate_ctx = accept_mandate_for_pending(l1_rpc, vault, sepolia_weth, loan_id, pending)
        mandate = get_mandate(vault, l1_rpc, loan_id)
        call_strike = int(mandate_ctx["callStrike"])
    else:
        call_strike = int(mandate["minCallStrike"])
    mandate_packet = _relay_and_handle_mandate_created(l1_rpc, l2_rpc, receiver, str(create_deposit["tx"]))
    _print_step(True, f"Loaded pending loan + mandate on L2 (loanId={loan_id}, guid={mandate_packet['guid']})")

    taker_nonce = 1_000_000 + loan_id
    runtime_updates = _resolve_l2_runtime_env(l2_rpc, l2, receiver)
    _ensure_receiver_is_submitter(l2_rpc, tsa, receiver)
    rfq_module = runtime_updates["RFQ_MODULE"]
    option_asset = runtime_updates["OPTION_ASSET"]
    base_tsa_addrs = re.findall(
        r"0x[a-fA-F0-9]{40}",
        cast_call(l2_rpc, tsa, "getBaseTSAAddresses()(address,address,address,address,address,address,address)"),
    )
    if len(base_tsa_addrs) < 6:
        raise RuntimeError("failed to parse BaseTSA addresses for RFQ e2e")
    manager = base_tsa_addrs[5]
    subaccount_id = int(cast_call(l2_rpc, tsa, "subAccount()(uint256)").split()[0])
    amount_decimal = _normalize_decimal_str(Decimal(int(pending["collateral"])) / Decimal(10**18))
    execute_direction = "buy"
    max_fee = "0"
    call_strike, put_strike, call_price, put_price = _find_valid_option_terms(
        l2_rpc=l2_rpc,
        tsa=tsa,
        option_risk_verifier=l2["l2OptionRiskVerifier"],
        option_asset=option_asset,
        manager=manager,
        expiry=int(pending["maturity"]),
        min_call_strike=call_strike,
        max_put_strike=int(pending["putStrike"]),
    )
    call_sub_id = _option_sub_id(int(pending["maturity"]), call_strike, True)
    put_sub_id = _option_sub_id(int(pending["maturity"]), put_strike, False)
    legs_for_signing = [
        {
            "instrument_name": "MOCK-CALL",
            "direction": "sell",
            "asset_address": option_asset,
            "sub_id": call_sub_id,
            "price": call_price,
            "amount": amount_decimal,
        },
        {
            "instrument_name": "MOCK-PUT",
            "direction": "buy",
            "asset_address": option_asset,
            "sub_id": put_sub_id,
            "price": put_price,
            "amount": amount_decimal,
        },
    ]
    expected_request = {
        "rfq_module": rfq_module,
        "subaccount_id": subaccount_id,
        "nonce": taker_nonce,
        "signer": tsa,
        "direction": execute_direction,
        "legs": [
            {
                "instrument_name": str(leg["instrument_name"]),
                "direction": str(leg["direction"]),
                "price": str(leg["price"]),
                "amount": str(leg["amount"]),
            }
            for leg in legs_for_signing
        ],
        "max_fee": max_fee,
        "label": "",
        "rfq_id": "mock-rfq-1",
        "quote_id": "mock-quote-1",
    }
    mock_server, mock_api_url, mock_state = _start_mock_derive_api(l2_rpc, tsa, taker_nonce, expected_request)

    tmpdir = Path(tempfile.mkdtemp(prefix="rfq-post-fill-attestation-e2e-"))
    l2_env = _write_env_with_updates(ROOT / ".env.l2.testnet", tmpdir / ".env.l2.fork", runtime_updates)
    keeper_state = tmpdir / "keeper_l2_state.json"
    rfq_trade_file = tmpdir / "rfq_trade.json"
    rfq_trade_file.write_text(
        json.dumps(
            {
                "loanId": loan_id,
                "takerNonce": taker_nonce,
                "callStrike": call_strike,
                "putStrike": put_strike,
                "expiry": int(pending["maturity"]),
                "asset": ZERO_ADDRESS,
                "amount": 0,
                "socketMessageId": ZERO_BYTES32,
                "quoteHash": ZERO_BYTES32,
                "realizedC": 0,
                "executeQuote": {
                    "rfqId": expected_request["rfq_id"],
                    "quoteId": expected_request["quote_id"],
                    "subaccountId": subaccount_id,
                    "direction": execute_direction,
                    "maxFee": max_fee,
                    "label": "",
                    "legs": [
                        {
                            "instrumentName": leg["instrument_name"],
                            "direction": leg["direction"],
                            "assetAddress": leg["asset_address"],
                            "subId": leg["sub_id"],
                            "price": leg["price"],
                            "amount": leg["amount"],
                        }
                        for leg in legs_for_signing
                    ],
                },
            },
            indent=2,
        )
    )
    try:
        keeper_out = _run_keeper_command(
            [
                "uv",
                "run",
                "python",
                str(ROOT / "ops/management/l2_keeper_handle_messages.py"),
                str(l2_env),
                "--state-file",
                str(keeper_state),
                "--rfq-trade-file",
                str(rfq_trade_file),
                "--receiver",
                receiver,
                "--derive-api-url",
                mock_api_url,
                "--start-block",
                str(int(run(["cast", "block-number", "--rpc-url", l2_rpc]))),
                "--broadcast",
                "--private-key",
                ANVIL_PK0,
                "--once",
                "--json",
            ]
        )
    finally:
        mock_server.shutdown()
        mock_server.server_close()

    rfq_handled = next(
        (
            item
            for item in keeper_out.get("handled", [])
            if isinstance(item, dict) and item.get("action") == "RfqExecuteAndConfirm" and item.get("status") == "sent"
        ),
        None,
    )
    if rfq_handled is None:
        raise RuntimeError(f"keeper did not process queued RFQ trade: {json.dumps(keeper_out)}")
    requests_seen = mock_state.get("requests", [])
    if not isinstance(requests_seen, list) or len(requests_seen) != 1:
        raise RuntimeError(f"unexpected mock Derive request count: {requests_seen!r}")

    _, action_data = _rfq_sign_payloads(loan_id, execute_direction, legs_for_signing, max_fee)
    action_tuple = _action_tuple(
        subaccount_id,
        taker_nonce,
        rfq_module,
        action_data,
        int(rfq_handled["deriveApi"]["expiry"]),
        tsa,
        tsa,
    )
    if cast_call(
        l2_rpc,
        tsa,
        "isActionSigned((uint256,uint256,address,bytes,uint256,address,address))(bool)",
        action_tuple,
    ).strip().lower() != "true":
        raise RuntimeError("RFQ taker action was not pre-signed onchain through the TSA")
    _print_step(True, "Executed RFQ quote via keeper and pre-signed taker action onchain")

    trade_packet = _relay_exact_lz_packet(l2_rpc, l1_rpc, rfq_handled["tradeConfirmedTx"])
    subaccount_id = int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0])
    inject_deposit_confirmed(
        l1_rpc,
        messenger,
        loan_id,
        vault,
        subaccount_id,
        sepolia_weth,
        int(pending["collateral"]),
        deposit_guid,
    )
    cast_send_pk(l1_rpc, vault, "finalizeLoan(uint256,bytes32,bytes32)", str(loan_id), deposit_guid, trade_packet["guid"])
    loan = get_loan(vault, l1_rpc, loan_id)
    if loan["borrower"].lower() == ZERO_ADDRESS:
        raise RuntimeError("finalizeLoan did not persist borrower")
    if loan["principal"] != int(pending["borrowAmount"]):
        raise RuntimeError(f"principal mismatch after finalizeLoan: expected {pending['borrowAmount']}, got {loan['principal']}")
    _print_step(True, "Finalized loan from relayed L2 TradeConfirmed packet")

    out = {
        "status": "success",
        "loanId": loan_id,
        "depositGuid": deposit_guid,
        "tradeGuid": trade_packet["guid"],
        "signActionViaPermitTx": rfq_handled["deriveApi"]["signActionViaPermitTx"],
        "recordTradeExecutedTx": rfq_handled["recordTradeExecutedTx"],
        "sendTradeConfirmedTx": rfq_handled["tradeConfirmedTx"],
    }
    path = tmpdir / "result.json"
    path.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Result artifact written: {path}")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
