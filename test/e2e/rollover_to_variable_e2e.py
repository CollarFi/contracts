#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[2]
THIS_DIR = Path(__file__).resolve().parent
ANVIL_PK0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDR0 = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
BORROWER_PK = "0x59c6995e998f97a5a0044966f0945382d77ad9e6f3c6f7f8b8d7a0f4f7f9d6f1"
SEED_USDC_HOLDER = "0xDf4fF02E2dDe3A08590829d7398Cc31B0255bAb5"
WETH_PRICE_USD = 3000 * 10**18
USDC_PRICE_USD = 1 * 10**30

app = typer.Typer(add_completion=False)


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout.strip()


def cast_call(rpc: str, to: str, sig: str, *args: str) -> str:
    return run(["cast", "call", to, sig, *args, "--rpc-url", rpc]).strip()


def cast_send_pk(rpc: str, to: str, sig: str, *args: str, private_key: str = ANVIL_PK0, value: str | None = None) -> str:
    cmd = ["cast", "send", to, sig, *args, "--rpc-url", rpc, "--private-key", private_key]
    if value:
        cmd += ["--value", value]
    return run(cmd)


def cast_send_from(rpc: str, frm: str, to: str, sig: str, *args: str, value: str | None = None) -> str:
    cmd = ["cast", "send", to, sig, *args, "--rpc-url", rpc, "--unlocked", "--from", frm]
    if value:
        cmd += ["--value", value]
    return run(cmd)


def _print_step(ok: bool, text: str) -> None:
    print(("✅" if ok else "❌") + " " + text)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _extract_address_from_forge_create(out: str) -> str:
    m = re.search(r"Deployed to:\s*(0x[a-fA-F0-9]{40})", out)
    if m:
        return m.group(1)
    j = re.search(r'"deployedTo"\s*:\s*"(0x[a-fA-F0-9]{40})"', out)
    if j:
        return j.group(1)
    addrs = re.findall(r"0x[a-fA-F0-9]{40}", out)
    if addrs:
        return addrs[-1]
    raise RuntimeError(f"failed to parse deployed address: {out[:300]}")


def _load_euler_core_addresses(chain_id: int = 11155111) -> dict:
    p = ROOT / "lib/euler-interfaces/addresses/test" / str(chain_id) / "CoreAddresses.json"
    return _load_json(p)


def _ensure_l1_sepolia_rpc(rpc: str) -> None:
    got = int(run(["cast", "chain-id", "--rpc-url", rpc]).strip())
    if got != 11155111:
        raise RuntimeError(f"expected L1 Sepolia fork (11155111), got {got}")


def _ensure_l2_derive_rpc(rpc: str) -> None:
    got = int(run(["cast", "chain-id", "--rpc-url", rpc]).strip())
    if got != 901:
        raise RuntimeError(f"expected L2 Derive fork (901), got {got}")


def _set_eth_balance(rpc: str, who: str, wei_hex: str = "0x3635C9ADC5DEA00000") -> None:
    run(["cast", "rpc", "anvil_setBalance", who, wei_hex, "--rpc-url", rpc])


def _has_code(rpc: str, addr: str) -> bool:
    return run(["cast", "code", addr, "--rpc-url", rpc]).strip().lower() != "0x"


def _require_code(rpc: str, addr: str, label: str) -> None:
    if not _has_code(rpc, addr):
        raise RuntimeError(
            f"missing code for {label} at {addr} on {rpc}; switch L1 fork URL to Sepolia source with Euler deployment (e.g. https://sepolia.gateway.tenderly.co)"
        )


def _deploy_contract(rpc: str, contract: str, *ctor_args: str) -> str:
    cmd = [
        "forge", "create", contract,
        "--rpc-url", rpc,
        "--private-key", ANVIL_PK0,
        "--broadcast",
    ]
    if ctor_args:
        cmd += ["--constructor-args", *ctor_args]
    out = run(cmd)
    return _extract_address_from_forge_create(out)


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


def _keccak_hex(hex_data: str) -> str:
    return run(["cast", "keccak", hex_data]).splitlines()[0].strip()


def _abi_encode(sig: str, *args: str) -> str:
    return run(["cast", "abi-encode", sig, *args]).strip()


def _force_set_erc20_balance_on_anvil(rpc: str, token: str, who: str, target_amount: int) -> bool:
    for slot in range(0, 16):
        key = _keccak_hex(_abi_encode("f(address,uint256)", who, str(slot)))
        run([
            "cast", "rpc", "anvil_setStorageAt", token, key, run(["cast", "to-bytes32", str(target_amount)]), "--rpc-url", rpc,
        ])
        bal = int(cast_call(rpc, token, "balanceOf(address)(uint256)", who).split()[0])
        if bal >= target_amount:
            return True
    return False


def _seed_usdc_liquidity(rpc: str, usdc: str, debt_vault: str, amount: int) -> None:
    if amount <= 0:
        return
    if _force_set_erc20_balance_on_anvil(rpc, usdc, ANVIL_ADDR0, amount):
        cast_send_pk(rpc, usdc, "approve(address,uint256)", debt_vault, str(amount), private_key=ANVIL_PK0)
        cast_send_pk(rpc, debt_vault, "deposit(uint256,address)", str(amount), ANVIL_ADDR0, private_key=ANVIL_PK0)
        return

    _set_eth_balance(rpc, SEED_USDC_HOLDER)
    cast_send_from(rpc, SEED_USDC_HOLDER, usdc, "approve(address,uint256)", debt_vault, str(amount))
    cast_send_from(rpc, SEED_USDC_HOLDER, debt_vault, "deposit(uint256,address)", str(amount), ANVIL_ADDR0)


def _run_fresh_loan_flow(l1_json: Path, l2_json: Path, l1_rpc: str, l2_rpc: str, collateral_asset: str) -> dict:
    out = run([
        "uv", "run", "python", str(ROOT / "test/e2e/fresh_loan_flow.py"),
        "--l1-json", str(l1_json),
        "--l2-json", str(l2_json),
        "--l1-rpc", l1_rpc,
        "--l2-rpc", l2_rpc,
        "--collateral-asset", collateral_asset,
        "--json",
    ])
    return json.loads(out)


def _borrower_address() -> str:
    return run(["cast", "wallet", "address", "--private-key", BORROWER_PK]).strip()


def _sign_no_prefix(digest_hex: str, private_key: str) -> str:
    return run(["cast", "wallet", "sign", "--no-hash", "--private-key", private_key, digest_hex]).strip()


def _inject_lz_message(l1_rpc: str, messenger: str, guid: str, message_payload: str) -> None:
    endpoint = cast_call(l1_rpc, messenger, "endpoint()(address)").splitlines()[0].strip()
    _set_eth_balance(l1_rpc, endpoint)
    sender_b32 = "0x" + "00" * 12 + ANVIL_ADDR0[2:]
    cast_send_from(
        l1_rpc,
        endpoint,
        messenger,
        "lzReceive((uint32,bytes32,uint64),bytes32,bytes,address,bytes)",
        f"(1234,{sender_b32},1)",
        guid,
        message_payload,
        "0x0000000000000000000000000000000000000000",
        "0x",
    )


def _set_time(rpc: str, ts: int) -> None:
    run(["cast", "rpc", "evm_setNextBlockTimestamp", hex(ts), "--rpc-url", rpc])
    run(["cast", "rpc", "evm_mine", "--rpc-url", rpc])


@app.command()
def main(
    l1_json: Path = typer.Option(Path("deployments/11155111/l1-e2e.json")),
    l2_json: Path = typer.Option(Path("deployments/901/l2-e2e.json")),
    l1_rpc: str = typer.Option("http://127.0.0.1:10111"),
    l2_rpc: str = typer.Option("http://127.0.0.1:10119"),
    sepolia_usdc: str = typer.Option("0x8537307810fC40F4073A12a38554D4Ff78EfFf41"),
    sepolia_weth: str = typer.Option("0x565810cbfa3Cf1390963E5aFa2fB953795686339"),
    usdc_seed: int = typer.Option(0),
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
    l1 = _load_json(l1_path).get("addrs", _load_json(l1_path))
    l2 = _load_json(l2_path).get("addrs", _load_json(l2_path))
    vault = l1["l1Vault"]
    messenger = l1["l1Messenger"]
    _print_step(True, f"Loaded deployments: L1 vault={vault} L2 receiver={l2.get('l2Receiver')}")

    weth_oracle = _deploy_contract(l1_rpc, "src/mocks/FixedPriceOracleMock.sol:FixedPriceOracleMock", str(WETH_PRICE_USD))
    usdc_oracle = _deploy_contract(l1_rpc, "src/mocks/FixedPriceOracleMock.sol:FixedPriceOracleMock", str(USDC_PRICE_USD))
    collateral_vault = _create_evault(l1_rpc, factory, implementation, sepolia_weth, weth_oracle, sepolia_usdc)
    debt_vault = _create_evault(l1_rpc, factory, implementation, sepolia_usdc, usdc_oracle, sepolia_usdc)
    _configure_vault_basics(l1_rpc, collateral_vault)
    _configure_vault_basics(l1_rpc, debt_vault)
    _set_ltv(l1_rpc, debt_vault, collateral_vault, 8500, 9000)
    _seed_usdc_liquidity(l1_rpc, sepolia_usdc, debt_vault, usdc_seed)
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
    m = re.search(r"\((0x[a-fA-F0-9]{40}),\s*(0x[a-fA-F0-9]{40}),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)", pending_raw)
    if not m:
        raise RuntimeError(f"failed to parse pending: {pending_raw}")
    p_borrower, p_asset, p_collateral, p_maturity, p_put, p_borrow = m.groups()
    if p_borrower.lower() != borrower.lower() or p_asset.lower() != sepolia_weth.lower():
        raise RuntimeError("pending deposit does not match expected borrower/asset")

    now_ts = int(run(["cast", "block", "latest", "--rpc-url", l1_rpc]).split("timestamp            ")[1].splitlines()[0].strip())
    rfq_expiry = now_ts + 3600
    mandate_deadline = now_ts + 1800
    rfq_tuple = f"({loan_id},{borrower},{sepolia_weth},{p_collateral},{p_maturity},{p_put},{p_borrow},{int(p_put)+1},{0},{rfq_expiry})"
    rfq_hash = cast_call(l1_rpc, vault, "hashBaselineRfq((uint256,address,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64))(bytes32)", rfq_tuple).splitlines()[0].strip()
    rfq_sig = _sign_no_prefix(rfq_hash, ANVIL_PK0)
    cast_send_pk(
        l1_rpc,
        vault,
        "acceptMandate(uint256,(uint256,address,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64),bytes,uint64)",
        str(loan_id),
        rfq_tuple,
        rfq_sig,
        str(mandate_deadline),
        private_key=BORROWER_PK,
        value=str(10**17),
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

    cast_send_pk(l1_rpc, vault, "convertToVariable(uint256,bytes32)", str(loan_id), collat_guid)
    converted = cast_call(l1_rpc, vault, "tryConvertReadyLoan(uint256)(bool)", str(loan_id)).strip().lower()
    if converted != "true":
        raise RuntimeError("tryConvertReadyLoan returned false")

    loan_raw = cast_call(l1_rpc, vault, "loans(uint256)(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint8,uint256,uint256,uint256,uint256)", str(loan_id))
    if ", 4," not in loan_raw and ",4," not in loan_raw:
        raise RuntimeError(f"loan not ACTIVE_VARIABLE: {loan_raw}")
    _print_step(True, "Converted to ACTIVE_VARIABLE")

    out = {
        "status": "success",
        "loanId": loan_id,
        "depositGuid": deposit_guid,
        "tradeGuid": trade_guid,
        "collateralGuid": collat_guid,
        "adapter": adapter,
        "positionImplementation": position_impl,
        "collateralVault": collateral_vault,
        "debtVault": debt_vault,
    }
    p = Path(tempfile.mkdtemp(prefix="rollover-var-e2e-")) / "result.json"
    p.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Result artifact written: {p}")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
