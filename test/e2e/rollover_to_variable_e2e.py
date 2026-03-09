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

WETH_PRICE_USD = 3000 * 10**18
USDC_PRICE_USD = 1 * 10**30

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
    force_set_erc20_allowance_on_anvil as _force_set_erc20_allowance_on_anvil,
    has_code as _has_code,
    inject_lz_message as _inject_lz_message,
    keccak_hex as _keccak_hex,
    load_json as _load_json,
    predict_next_create_address as _predict_next_create_address,
    print_step as _print_step,
    require_code as _require_code,
    run,
    run_fresh_loan_flow as _run_fresh_loan_flow,
    seed_l1_liquidity_vault as _seed_l1_liquidity_vault,
    seed_usdc_liquidity as _seed_usdc_liquidity,
    set_eth_balance as _set_eth_balance,
    set_time as _set_time,
    sign_no_prefix as _sign_no_prefix,
)
from loan_flow_helpers import get_mandate

def _load_euler_core_addresses(chain_id: int = 11155111) -> dict:
    p = ROOT / "lib/euler-interfaces/addresses/test" / str(chain_id) / "CoreAddresses.json"
    if p.exists():
        return _load_json(p)

    # Fallback for environments where euler-interfaces address book is not checked out.
    if chain_id == 11155111:
        return {
            "evc": "0x28b0C8B389c3e39A4AFe089A6810A2e7Bc3C551A",
            "eVaultFactory": "0xEB07789D76392302dc9181Aca1e07836F9257B5a",
            "eVaultImplementation": "0x3da1BBD2fC6BC1c7893d553D687603F3B8723085",
            "protocolConfig": "0x98d70B9e97C918ecBbF7EC1751CF7EBF9728a5b6",
        }

    raise RuntimeError(f"missing Euler core address book: {p}")

def _create_evault(rpc: str, factory: str, implementation: str, asset: str, oracle: str, unit_of_account: str) -> str:
    trailing = "0x" + asset[2:] + oracle[2:] + unit_of_account[2:]
    cast_send_pk(rpc, factory, "createProxy(address,bool,bytes)(address)", implementation, "true", trailing)
    length = int(cast_call(rpc, factory, "getProxyListLength()(uint256)").split()[0])
    return cast_call(rpc, factory, "proxyList(uint256)(address)", str(length - 1)).splitlines()[0].strip()

def _configure_vault_basics(rpc: str, vault: str) -> None:
    cast_send_pk(rpc, vault, "setHookConfig(address,uint32)", "0x0000000000000000000000000000000000000000", "0")
    cast_send_pk(rpc, vault, "setCaps(uint16,uint16)", "0", "0")

def _set_ltv(rpc: str, debt_vault: str, collateral_vault: str, borrow_ltv_bps: int = 8500, liq_ltv_bps: int = 9000) -> None:
    cast_send_pk(rpc, debt_vault, "setLTV(address,uint16,uint16,uint32)", collateral_vault, str(borrow_ltv_bps), str(liq_ltv_bps), "0")

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

    core = _load_euler_core_addresses(11155111)
    evc = core["evc"]
    factory = core["eVaultFactory"]
    implementation = core["eVaultImplementation"]
    protocol_config = core["protocolConfig"]
    _require_code(l1_rpc, evc, "Euler EVC")
    _require_code(l1_rpc, factory, "Euler eVaultFactory")
    _require_code(l1_rpc, implementation, "Euler eVaultImplementation")
    _require_code(l1_rpc, protocol_config, "Euler protocolConfig")
    _require_code(l1_rpc, sepolia_weth, "Sepolia WETH collateral")
    _require_code(l1_rpc, sepolia_usdc, "Sepolia USDC debt")
    _print_step(True, f"Euler core loaded (evc={evc}, factory={factory})")

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

    weth_oracle = _deploy_contract(l1_rpc, "src/mocks/FixedPriceOracleMock.sol:FixedPriceOracleMock", str(WETH_PRICE_USD))
    usdc_oracle = _deploy_contract(l1_rpc, "src/mocks/FixedPriceOracleMock.sol:FixedPriceOracleMock", str(USDC_PRICE_USD))
    collateral_vault = _create_evault(l1_rpc, factory, implementation, sepolia_weth, weth_oracle, sepolia_usdc)
    debt_vault = _create_evault(l1_rpc, factory, implementation, sepolia_usdc, usdc_oracle, sepolia_usdc)
    _configure_vault_basics(l1_rpc, collateral_vault)
    _configure_vault_basics(l1_rpc, debt_vault)
    _set_ltv(l1_rpc, debt_vault, collateral_vault, 8500, 9000)
    _seed_usdc_liquidity(l1_rpc, sepolia_usdc, debt_vault, usdc_seed)
    _seed_l1_liquidity_vault(l1_rpc, sepolia_usdc, l1_liquidity_vault, usdc_seed)
    _print_step(True, "Bootstrapped EVaults/oracles/liquidity")

    adapter = _deploy_contract(
        l1_rpc,
        "src/adapters/EulerLendingAdapter.sol:EulerLendingAdapter",
        evc,
        sepolia_weth,
        collateral_vault,
        sepolia_usdc,
        debt_vault,
    )
    position_impl = _deploy_contract(l1_rpc, "src/adapters/VariableLoanPosition.sol:VariableLoanPosition")
    cast_send_pk(l1_rpc, vault, "setLendingAdapter(address)", adapter)
    cast_send_pk(l1_rpc, vault, "setVariableLoanPositionImplementation(address)", position_impl)
    _print_step(True, f"Wired adapter={adapter} and positionImpl={position_impl}")

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
    apr = int(cast_call(l1_rpc, vault, "originationFeeApr()(uint256)").split()[0])
    year = 365 * 24 * 3600
    fixed_interest = ((int(p_borrow) * apr) // 10**18) * (int(p_maturity) - now_ts) // year
    mandate = get_mandate(vault, l1_rpc, loan_id)
    subaccount_id = cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0]
    if mandate["borrower"] == "0x0000000000000000000000000000000000000000":
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
        default_opts = cast_call(l1_rpc, lz_messenger, "defaultOptions()(bytes)").splitlines()[0].strip()
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
    else:
        call_strike = int(mandate["minCallStrike"])
        _print_step(True, "Loaded atomic mandate on L1")

    trade_data = _abi_encode("f(uint256,uint256,uint64,int256)", str(call_strike), str(p_put), str(p_maturity), "0")
    trade_msg = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(5,{loan_id},0x0000000000000000000000000000000000000000,0,{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,{trade_data})",
    )
    trade_guid = "0x" + format(10_000_000 + loan_id, "064x")
    _inject_lz_message(l1_rpc, messenger, trade_guid, trade_msg)

    # Fresh-flow ACK on fork can carry the L2-side underlying asset; rewrite the cached
    # DepositConfirmed payload for this guid to match L1 collateral validation in finalize.
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

    predicted_position = _predict_next_create_address(l1_rpc, vault)
    # Euler fork quirk: collateral deposit path can pull from the position account during EVC call,
    # so preseed both allowance and minimal balance on the predicted clone address.
    if not _force_set_erc20_allowance_on_anvil(l1_rpc, sepolia_weth, predicted_position, collateral_vault, 2**256 - 1):
        raise RuntimeError(f"failed to preseed collateral allowance for variable position {predicted_position}")
    _ensure_token_balance(l1_rpc, sepolia_weth, predicted_position, int(p_collateral))

    cast_send_pk(l1_rpc, vault, "tryConvertReadyLoan(uint256)(bool)", str(loan_id), private_key=ANVIL_PK0)

    loan_raw = cast_call(l1_rpc, vault, "loans(uint256)(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint8,uint256,uint256,uint256,uint256)", str(loan_id))
    loan_lines = [ln.strip() for ln in loan_raw.splitlines() if ln.strip()]
    loan_state = int(loan_lines[8].split()[0]) if len(loan_lines) > 8 else -1

    if loan_state != 3:
        # Retry once to absorb occasional fork timing/liquidity propagation flake.
        cast_send_pk(l1_rpc, vault, "tryConvertReadyLoan(uint256)(bool)", str(loan_id), private_key=ANVIL_PK0)
        loan_raw = cast_call(l1_rpc, vault, "loans(uint256)(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint8,uint256,uint256,uint256,uint256)", str(loan_id))
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
        "collateralVault": collateral_vault,
        "debtVault": debt_vault,
    }
    p = Path(tempfile.mkdtemp(prefix="rollover-var-e2e-")) / "result.json"
    p.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Result artifact written: {p}")

    print("\nResult: SUCCESS")

if __name__ == "__main__":
    app()
