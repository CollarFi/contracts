#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[2]
THIS_DIR = Path(__file__).resolve().parent

import sys
sys.path.insert(0, str(THIS_DIR))
from defaults import (
    L1_ANVIL_PORT,
    L1_ARTIFACT_JSON,
    L1_COLLATERAL_ASSET,
    L1_DEBT_ASSET,
    L1_WETH_SOCKET_CONNECTOR,
    L1_WETH_SOCKET_VAULT,
    L2_ANVIL_PORT,
    L2_ARTIFACT_JSON,
)

MORPHO_LLTV = int(0.85 * 10**18)
# Morpho oracle scale is 1e36. For 18-dec collateral vs 6-dec loan token,
# 1 collateral worth 3000 loan-token units is represented as 3000 * 1e24.
MORPHO_COLLATERAL_PRICE = 3000 * 10**24

app = typer.Typer(add_completion=False)

from common import (
    ANVIL_ADDR0,
    ANVIL_PK0,
    BORROWER_PK,
    SEED_USDC_HOLDER,
    abi_encode as _abi_encode,
    borrower_address as _borrower_address,
    cast_call,
    cast_send_from,
    cast_send_pk,
    deploy_contract as _deploy_contract,
    ensure_keeper_role as _ensure_keeper_role,
    ensure_l1_sepolia_rpc as _ensure_l1_sepolia_rpc,
    ensure_l2_derive_rpc as _ensure_l2_derive_rpc,
    ensure_liquidity_vault_role as _ensure_liquidity_vault_role,
    ensure_live_deployments as _ensure_live_deployments,
    ensure_token_balance as _ensure_token_balance,
    has_code as _has_code,
    inject_lz_message as _inject_lz_message,
    keccak_hex as _keccak_hex,
    print_step as _print_step,
    require_code as _require_code,
    run,
    run_fresh_loan_flow as _run_fresh_loan_flow,
    seed_l1_liquidity_vault as _seed_l1_liquidity_vault,
    set_eth_balance as _set_eth_balance,
    set_time as _set_time,
    sign_no_prefix as _sign_no_prefix,
)

def _morpho_market_tuple(loan_token: str, collateral_token: str, oracle: str, irm: str, lltv: int) -> str:
    return f"({loan_token},{collateral_token},{oracle},{irm},{lltv})"

def _morpho_market_id(loan_token: str, collateral_token: str, oracle: str, irm: str, lltv: int) -> str:
    packed = _abi_encode("f(address,address,address,address,uint256)", loan_token, collateral_token, oracle, irm, str(lltv))
    return _keccak_hex(packed)

def _deploy_morpho_market(rpc: str, usdc: str, collateral: str, seed_amount: int) -> dict:
    morpho = _deploy_contract(rpc, "lib/morpho-blue/src/Morpho.sol:Morpho", ANVIL_ADDR0)
    oracle = _deploy_contract(rpc, "lib/morpho-blue/src/mocks/OracleMock.sol:OracleMock")
    irm = _deploy_contract(rpc, "lib/morpho-blue/src/mocks/IrmMock.sol:IrmMock")

    cast_send_pk(rpc, oracle, "setPrice(uint256)", str(MORPHO_COLLATERAL_PRICE))
    cast_send_pk(rpc, morpho, "enableIrm(address)", irm)
    cast_send_pk(rpc, morpho, "enableLltv(uint256)", str(MORPHO_LLTV))

    market = _morpho_market_tuple(usdc, collateral, oracle, irm, MORPHO_LLTV)
    cast_send_pk(rpc, morpho, "createMarket((address,address,address,address,uint256))", market)

    market_id = _morpho_market_id(usdc, collateral, oracle, irm, MORPHO_LLTV)

    def _read_total_supply_assets() -> int:
        market_state = cast_call(rpc, morpho, "market(bytes32)((uint128,uint128,uint128,uint128,uint128,uint128))", market_id)
        m_supply = re.search(r"\d+", market_state)
        return int(m_supply.group(0)) if m_supply else 0

    if seed_amount > 0:
        # Path A: anvil storage-topup + supply from default funded key.
        _ensure_token_balance(rpc, usdc, ANVIL_ADDR0, seed_amount)
        cast_send_pk(rpc, usdc, "approve(address,uint256)", morpho, str(seed_amount), private_key=ANVIL_PK0)
        cast_send_pk(
            rpc,
            morpho,
            "supply((address,address,address,address,uint256),uint256,uint256,address,bytes)(uint256,uint256)",
            market,
            str(seed_amount),
            "0",
            ANVIL_ADDR0,
            "0x",
            private_key=ANVIL_PK0,
        )

        # Path B fallback: impersonate known USDC holder if market still not funded.
        if _read_total_supply_assets() < seed_amount:
            _set_eth_balance(rpc, SEED_USDC_HOLDER)
            _ensure_token_balance(rpc, usdc, SEED_USDC_HOLDER, seed_amount)
            cast_send_from(rpc, SEED_USDC_HOLDER, usdc, "approve(address,uint256)", morpho, str(seed_amount))
            cast_send_from(
                rpc,
                SEED_USDC_HOLDER,
                morpho,
                "supply((address,address,address,address,uint256),uint256,uint256,address,bytes)(uint256,uint256)",
                market,
                str(seed_amount),
                "0",
                ANVIL_ADDR0,
                "0x",
            )

    total_supply_assets = _read_total_supply_assets()
    if total_supply_assets < seed_amount:
        raise RuntimeError(
            f"failed to seed Morpho market liquidity: target={seed_amount}, actualSupply={total_supply_assets}"
        )

    return {
        "morpho": morpho,
        "oracle": oracle,
        "irm": irm,
        "lltv": MORPHO_LLTV,
        "marketId": market_id,
        "totalSupplyAssets": total_supply_assets,
    }

@app.command()
def main(
    l1_json: Path = typer.Option(L1_ARTIFACT_JSON),
    l2_json: Path = typer.Option(L2_ARTIFACT_JSON),
    l1_rpc: str = typer.Option(f"http://127.0.0.1:{L1_ANVIL_PORT}"),
    l2_rpc: str = typer.Option(f"http://127.0.0.1:{L2_ANVIL_PORT}"),
    sepolia_usdc: str = typer.Option(L1_DEBT_ASSET),
    sepolia_weth: str = typer.Option(L1_COLLATERAL_ASSET),
    usdc_seed: int = typer.Option(3_000_000_000),
    auto_redeploy: bool = typer.Option(True, "--auto-redeploy/--no-auto-redeploy"),
):
    print("=== collar.fi rollover-to-variable e2e ===")

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
        _print_step(True, "Detected stale deployment artifacts/runtime; ran deployment_e2e to refresh")

    vault = l1["l1Vault"]
    messenger = l1["l1Messenger"]
    l1_liquidity_vault = l1["l1LiquidityVault"]
    _print_step(True, f"Loaded deployments: L1 vault={vault} L2 receiver={l2.get('l2Receiver')}")

    _ensure_liquidity_vault_role(l1_rpc, vault)
    _ensure_keeper_role(l1_rpc, vault)

    morpho_market = _deploy_morpho_market(l1_rpc, sepolia_usdc, sepolia_weth, usdc_seed)
    _seed_l1_liquidity_vault(l1_rpc, sepolia_usdc, l1_liquidity_vault, usdc_seed)
    _print_step(
        True,
        f"Bootstrapped Morpho market (morpho={morpho_market['morpho']}, marketId={morpho_market['marketId']})",
    )

    adapter = _deploy_contract(
        l1_rpc,
        "src/adapters/MorphoBlueLendingAdapter.sol:MorphoBlueLendingAdapter",
        morpho_market["morpho"],
        sepolia_weth,
        sepolia_usdc,
        morpho_market["oracle"],
        morpho_market["irm"],
        str(morpho_market["lltv"]),
    )
    position_impl = _deploy_contract(l1_rpc, "src/adapters/VariableLoanPosition.sol:VariableLoanPosition")
    cast_send_pk(l1_rpc, vault, "setLendingAdapter(address)", adapter)
    cast_send_pk(l1_rpc, vault, "setVariableLoanPositionImplementation(address)", position_impl)
    _print_step(True, f"Wired Morpho adapter={adapter} and positionImpl={position_impl}")

    flow = _run_fresh_loan_flow(l1_json, l2_json, l1_rpc, l2_rpc, sepolia_weth)
    if not flow.get("ok"):
        raise RuntimeError("fresh_loan_flow failed")
    verify = next((s.get("result") for s in flow.get("steps", []) if s.get("step") == "verify_expected_state"), None)
    if not isinstance(verify, dict):
        raise RuntimeError("fresh_loan_flow verify result missing")
    loan_id = int(verify["loanId"])
    deposit_guid = verify["l2ToL1Guid"]
    _print_step(True, f"Created pending loan via fresh flow (loanId={loan_id})")

    borrower = _borrower_address()
    pending_raw = cast_call(l1_rpc, vault, "pendingDeposits(uint256)((address,address,uint256,uint256,uint256,uint256))", str(loan_id))
    m = re.search(
        r"\((0x[a-fA-F0-9]{40}),\s*(0x[a-fA-F0-9]{40}),\s*(\d+)(?:\s*\[[^\]]+\])?,\s*(\d+)(?:\s*\[[^\]]+\])?,\s*(\d+)(?:\s*\[[^\]]+\])?,\s*(\d+)(?:\s*\[[^\]]+\])?\)",
        pending_raw,
        flags=re.S,
    )
    if not m:
        raise RuntimeError(f"failed to parse pending: {pending_raw}")
    p_borrower, p_asset, p_collateral, p_maturity, p_put, p_borrow = m.groups()
    if p_borrower.lower() != borrower.lower() or p_asset.lower() != sepolia_weth.lower():
        raise RuntimeError("pending deposit does not match expected borrower/asset")

    block_latest = json.loads(run(["cast", "block", "latest", "--rpc-url", l1_rpc, "--json"]))
    ts_raw = block_latest.get("timestamp")
    now_ts = int(ts_raw, 0) if isinstance(ts_raw, str) else int(ts_raw)
    rfq_expiry = now_ts + 3600
    mandate_deadline = now_ts + 1800
    call_strike = int(p_put) + 1
    rfq_tuple = (
        f"({loan_id},{sepolia_weth},{p_collateral},{p_maturity},{p_put},{call_strike},"
        f"{p_borrow},0,{rfq_expiry},{borrower},0)"
    )
    rfq_hash = cast_call(
        l1_rpc,
        vault,
        "hashBaselineRfq((uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64,address,uint256))(bytes32)",
        rfq_tuple,
    ).splitlines()[0].strip()
    rfq_sig = _sign_no_prefix(rfq_hash, ANVIL_PK0)

    lz_messenger = cast_call(l1_rpc, vault, "lzMessenger()(address)").splitlines()[0].strip()
    subaccount_id = int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0])
    default_opts = cast_call(l1_rpc, lz_messenger, "defaultOptions()(bytes)").splitlines()[0].strip()
    apr = int(cast_call(l1_rpc, vault, "originationFeeApr()(uint256)").split()[0])
    year = 365 * 24 * 3600
    fixed_interest = ((int(p_borrow) * apr) // 10**18) * (int(p_maturity) - now_ts) // year
    max_roll_ltv = int(cast_call(l1_rpc, vault, "maxRollLtv()(uint256)").split()[0])
    strike_scale = int(cast_call(l1_rpc, vault, "strikeScale(address)(uint256)", sepolia_weth).split()[0])
    mandate_data = _abi_encode(
        "f(address,uint256,uint256,uint256,uint256,uint256,uint256,uint64,uint64)",
        borrower,
        str(call_strike),
        p_put,
        "0",
        str(fixed_interest),
        str(max_roll_ltv),
        str(strike_scale),
        p_maturity,
        str(mandate_deadline),
    )
    quote_msg = (
        f"(6,{loan_id},{sepolia_weth},{p_borrow},{vault},{subaccount_id},"
        f"0x{'00'*32},0,0x{'00'*32},0,{mandate_data})"
    )
    lz_fee = int(re.search(
        r"\d+",
        cast_call(
            l1_rpc,
            lz_messenger,
            "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
            quote_msg,
            default_opts,
        ),
    ).group(0))
    cast_send_pk(
        l1_rpc,
        vault,
        "acceptMandate(uint256,(uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64,address,uint256),bytes,uint64)",
        str(loan_id),
        rfq_tuple,
        rfq_sig,
        str(mandate_deadline),
        private_key=BORROWER_PK,
        value=str(lz_fee),
    )
    _print_step(True, "Accepted mandate on L1")

    subaccount_id = cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0]
    trade_data = _abi_encode("f(uint256,uint256,uint64,int256)", str(int(p_put) + 1), str(p_put), str(p_maturity), "0")
    trade_msg = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(5,{loan_id},0x0000000000000000000000000000000000000000,0,{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,{trade_data})",
    )
    trade_guid = "0x" + format(10_000_000 + loan_id, "064x")
    _inject_lz_message(l1_rpc, messenger, trade_guid, trade_msg)

    deposit_confirm_msg = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(3,{loan_id},{sepolia_weth},{p_collateral},{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,0x)",
    )
    _inject_lz_message(l1_rpc, messenger, deposit_guid, deposit_confirm_msg)

    cast_send_pk(l1_rpc, vault, "finalizeLoan(uint256,bytes32,bytes32)", str(loan_id), deposit_guid, trade_guid)
    _print_step(True, "Finalized loan to ACTIVE_ZERO_COST")

    maturity = int(p_maturity)
    _set_time(l1_rpc, maturity + 1)
    collat_msg = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(4,{loan_id},{sepolia_weth},{p_collateral},{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,0x)",
    )
    collat_guid = "0x" + format(20_000_000 + loan_id, "064x")
    _inject_lz_message(l1_rpc, messenger, collat_guid, collat_msg)

    cast_send_pk(l1_rpc, vault, "settleLoan(uint256,uint8,bytes32)", str(loan_id), "1", collat_guid)
    _ensure_token_balance(l1_rpc, sepolia_weth, vault, int(p_collateral))
    cast_send_pk(l1_rpc, vault, "tryConvertReadyLoan(uint256)(bool)", str(loan_id), private_key=ANVIL_PK0)

    loan_raw = cast_call(
        l1_rpc,
        vault,
        "loans(uint256)(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint8,uint256,uint256,uint256,uint256)",
        str(loan_id),
    )
    loan_lines = [ln.strip() for ln in loan_raw.splitlines() if ln.strip()]
    loan_state = int(loan_lines[8].split()[0]) if len(loan_lines) > 8 else -1

    if loan_state != 3:
        cast_send_pk(l1_rpc, vault, "tryConvertReadyLoan(uint256)(bool)", str(loan_id), private_key=ANVIL_PK0)
        loan_raw = cast_call(
            l1_rpc,
            vault,
            "loans(uint256)(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint8,uint256,uint256,uint256,uint256)",
            str(loan_id),
        )
        loan_lines = [ln.strip() for ln in loan_raw.splitlines() if ln.strip()]
        loan_state = int(loan_lines[8].split()[0]) if len(loan_lines) > 8 else -1

    position_addr = cast_call(l1_rpc, vault, "variableLoanPosition(uint256)(address)", str(loan_id)).splitlines()[0].strip()

    if loan_state != 3:
        available_liquidity = -1
        if position_addr != "0x0000000000000000000000000000000000000000" and _has_code(l1_rpc, position_addr):
            available_liquidity = int(cast_call(l1_rpc, position_addr, "availableLiquidity()(uint256)").split()[0])
        required_debt = int(p_borrow) + fixed_interest
        raise RuntimeError(
            f"loan not ACTIVE_VARIABLE (state={loan_state}, position={position_addr}, availableLiquidity={available_liquidity}, requiredDebt={required_debt}): {loan_raw}"
        )
    _print_step(True, "Converted to ACTIVE_VARIABLE")

    out = {
        "status": "success",
        "loanId": loan_id,
        "depositGuid": deposit_guid,
        "tradeGuid": trade_guid,
        "collateralGuid": collat_guid,
        "adapter": adapter,
        "positionImplementation": position_impl,
        "positionAddress": position_addr,
        "morpho": morpho_market["morpho"],
        "morphoOracle": morpho_market["oracle"],
        "morphoIrm": morpho_market["irm"],
        "morphoMarketId": morpho_market["marketId"],
    }
    p = Path(tempfile.mkdtemp(prefix="rollover-var-e2e-")) / "result.json"
    p.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Result artifact written: {p}")

    print("\nResult: SUCCESS")

if __name__ == "__main__":
    app()
