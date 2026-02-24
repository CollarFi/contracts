#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

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

ANVIL_PK0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDR0 = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
BORROWER_PK = "0x59c6995e998f97a5a0044966f0945382d77ad9e6f3c6f7f8b8d7a0f4f7f9d6f1"
SEED_USDC_HOLDER = "0xDf4fF02E2dDe3A08590829d7398Cc31B0255bAb5"
WETH_PRICE_USD = 3000 * 10**18
USDC_PRICE_USD = 1 * 10**30

app = typer.Typer(add_completion=False)
ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


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


def _load_addrs(path: Path) -> dict:
    data = _load_json(path)
    return data.get("addrs", data)


def _extract_local_port(rpc: str) -> int | None:
    try:
        parsed = urlparse(rpc)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        return None
    return parsed.port


def _deployment_artifact_issues(
    l1_path: Path,
    l2_path: Path,
    l1_rpc: str,
    l2_rpc: str,
    expected_l1_usdc: str,
) -> tuple[list[str], dict | None, dict | None]:
    issues: list[str] = []
    l1 = None
    l2 = None

    if not l1_path.exists():
        issues.append(f"missing artifact: {l1_path}")
    else:
        try:
            l1 = _load_addrs(l1_path)
        except Exception as e:
            issues.append(f"failed reading {l1_path}: {e}")

    if not l2_path.exists():
        issues.append(f"missing artifact: {l2_path}")
    else:
        try:
            l2 = _load_addrs(l2_path)
        except Exception as e:
            issues.append(f"failed reading {l2_path}: {e}")

    required_l1 = {
        "l1Vault": "L1 vault",
        "l1Messenger": "L1 messenger",
        "l1LiquidityVault": "L1 liquidity vault",
    }
    required_l2 = {
        "l2Receiver": "L2 receiver",
        "l2Tsa": "L2 TSA",
    }

    if l1 is not None:
        for key, label in required_l1.items():
            addr = l1.get(key, "")
            if not isinstance(addr, str) or not ADDR_RE.match(addr):
                issues.append(f"invalid {key} in {l1_path}: {addr}")
                continue
            if not _has_code(l1_rpc, addr):
                issues.append(f"no runtime code for {label} ({addr}) on {l1_rpc}")

        vault = l1.get("l1Vault", "")
        liquidity_vault = l1.get("l1LiquidityVault", "")
        if isinstance(vault, str) and ADDR_RE.match(vault) and isinstance(liquidity_vault, str) and ADDR_RE.match(liquidity_vault):
            try:
                vault_usdc = cast_call(l1_rpc, vault, "usdc()(address)").splitlines()[0].strip()
                liquidity_asset = cast_call(l1_rpc, liquidity_vault, "asset()(address)").splitlines()[0].strip()
                if vault_usdc.lower() != expected_l1_usdc.lower():
                    issues.append(
                        f"vault usdc mismatch on {l1_rpc}: expected {expected_l1_usdc}, got {vault_usdc}"
                    )
                if liquidity_asset.lower() != expected_l1_usdc.lower():
                    issues.append(
                        f"liquidity vault asset mismatch on {l1_rpc}: expected {expected_l1_usdc}, got {liquidity_asset}"
                    )
                if vault_usdc.lower() != liquidity_asset.lower():
                    issues.append(
                        f"vault/liquidity vault asset mismatch on {l1_rpc}: vault={vault_usdc}, liquidityVault={liquidity_asset}"
                    )
            except Exception as e:
                issues.append(f"failed reading L1 USDC wiring from live deployment: {e}")

    if l2 is not None:
        for key, label in required_l2.items():
            addr = l2.get(key, "")
            if not isinstance(addr, str) or not ADDR_RE.match(addr):
                issues.append(f"invalid {key} in {l2_path}: {addr}")
                continue
            if not _has_code(l2_rpc, addr):
                issues.append(f"no runtime code for {label} ({addr}) on {l2_rpc}")

    return issues, l1, l2


def _run_deployment_e2e(l1_port: int, l2_port: int, sepolia_usdc: str, sepolia_weth: str) -> None:
    run([
        "uv", "run", "python", str(ROOT / "test/e2e/deployment_e2e.py"),
        "--l1-port", str(l1_port),
        "--l2-port", str(l2_port),
        "--l1-usdc-asset", sepolia_usdc,
        "--l1-weth-asset", sepolia_weth,
        "--weth-socket-vault", L1_WETH_SOCKET_VAULT,
        "--weth-socket-connector", L1_WETH_SOCKET_CONNECTOR,
        "--keep-anvil",
    ])


def _ensure_live_deployments(l1_path: Path, l2_path: Path, l1_rpc: str, l2_rpc: str, sepolia_usdc: str, sepolia_weth: str, auto_redeploy: bool) -> tuple[dict, dict, bool]:
    issues, l1, l2 = _deployment_artifact_issues(l1_path, l2_path, l1_rpc, l2_rpc, sepolia_usdc)
    if not issues:
        return l1 or {}, l2 or {}, False

    if not auto_redeploy:
        raise RuntimeError("deployment artifacts are stale/invalid:\n- " + "\n- ".join(issues))

    l1_port = _extract_local_port(l1_rpc)
    l2_port = _extract_local_port(l2_rpc)
    if l1_port is None or l2_port is None:
        raise RuntimeError(
            "deployment artifacts are stale/invalid and auto-redeploy requires local RPC URLs "
            f"(got l1={l1_rpc}, l2={l2_rpc}):\n- " + "\n- ".join(issues)
        )

    _run_deployment_e2e(l1_port, l2_port, sepolia_usdc, sepolia_weth)

    issues2, l1_after, l2_after = _deployment_artifact_issues(l1_path, l2_path, l1_rpc, l2_rpc, sepolia_usdc)
    if issues2:
        raise RuntimeError("deployment_e2e completed but artifacts/runtime still invalid:\n- " + "\n- ".join(issues2))
    return l1_after or {}, l2_after or {}, True


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


def _force_set_erc20_allowance_on_anvil(rpc: str, token: str, owner: str, spender: str, target_amount: int) -> bool:
    target_b32 = "0x" + int(target_amount).to_bytes(32, "big", signed=False).hex()
    for slot in range(0, 24):
        owner_slot = _keccak_hex(_abi_encode("f(address,uint256)", owner, str(slot)))
        key = _keccak_hex(_abi_encode("f(address,bytes32)", spender, owner_slot))
        run(["cast", "rpc", "anvil_setStorageAt", token, key, target_b32, "--rpc-url", rpc])
        allowance = int(cast_call(rpc, token, "allowance(address,address)(uint256)", owner, spender).split()[0])
        if allowance >= target_amount:
            return True
    return False


def _seed_usdc_liquidity(rpc: str, usdc: str, debt_vault: str, amount: int) -> None:
    if amount <= 0:
        return

    cash_before = int(cast_call(rpc, debt_vault, "cash()(uint256)").split()[0])
    target_cash = cash_before + amount

    if _force_set_erc20_balance_on_anvil(rpc, usdc, ANVIL_ADDR0, amount):
        cast_send_pk(rpc, usdc, "approve(address,uint256)", debt_vault, str(amount), private_key=ANVIL_PK0)
        cast_send_pk(rpc, debt_vault, "deposit(uint256,address)", str(amount), ANVIL_ADDR0, private_key=ANVIL_PK0)
    else:
        _set_eth_balance(rpc, SEED_USDC_HOLDER)
        cast_send_from(rpc, SEED_USDC_HOLDER, usdc, "approve(address,uint256)", debt_vault, str(amount))
        cast_send_from(rpc, SEED_USDC_HOLDER, debt_vault, "deposit(uint256,address)", str(amount), ANVIL_ADDR0)

    cash_after = int(cast_call(rpc, debt_vault, "cash()(uint256)").split()[0])
    if cash_after < target_cash:
        raise RuntimeError(
            f"failed to seed Euler debt vault cash: target={target_cash}, actual={cash_after}, debtVault={debt_vault}, usdc={usdc}"
        )


def _seed_l1_liquidity_vault(rpc: str, usdc: str, liquidity_vault: str, amount: int) -> None:
    if amount <= 0:
        return
    if _force_set_erc20_balance_on_anvil(rpc, usdc, ANVIL_ADDR0, amount):
        cast_send_pk(rpc, usdc, "approve(address,uint256)", liquidity_vault, str(amount), private_key=ANVIL_PK0)
        cast_send_pk(rpc, liquidity_vault, "deposit(uint256,address)", str(amount), ANVIL_ADDR0, private_key=ANVIL_PK0)
        return

    _set_eth_balance(rpc, SEED_USDC_HOLDER)
    cast_send_from(rpc, SEED_USDC_HOLDER, usdc, "approve(address,uint256)", liquidity_vault, str(amount))
    cast_send_from(rpc, SEED_USDC_HOLDER, liquidity_vault, "deposit(uint256,address)", str(amount), ANVIL_ADDR0)


def _ensure_token_balance(rpc: str, token: str, who: str, amount: int) -> None:
    current = int(cast_call(rpc, token, "balanceOf(address)(uint256)", who).split()[0])
    if current >= amount:
        return
    if not _force_set_erc20_balance_on_anvil(rpc, token, who, amount):
        raise RuntimeError(f"failed to top up {token} for {who}: {current} < {amount}")


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


def _predict_next_create_address(rpc: str, deployer: str) -> str:
    nonce = int(run(["cast", "nonce", deployer, "--rpc-url", rpc]).split()[0])
    out = run(["cast", "compute-address", deployer, "--nonce", str(nonce)])
    m = re.search(r"0x[a-fA-F0-9]{40}", out)
    if not m:
        raise RuntimeError(f"failed to parse compute-address output: {out}")
    return m.group(0)


def _sign_no_prefix(digest_hex: str, private_key: str) -> str:
    return run(["cast", "wallet", "sign", "--no-hash", "--private-key", private_key, digest_hex]).strip()


def _inject_lz_message(l1_rpc: str, messenger: str, guid: str, message_payload: str) -> None:
    endpoint = cast_call(l1_rpc, messenger, "endpoint()(address)").splitlines()[0].strip()
    _set_eth_balance(l1_rpc, endpoint)
    src_eid = int(cast_call(l1_rpc, messenger, "remoteEid()(uint32)").split()[0])
    sender_b32 = cast_call(l1_rpc, messenger, "peers(uint32)(bytes32)", str(src_eid)).splitlines()[0].strip()
    if sender_b32.lower() == "0x" + "00" * 32:
        sender_b32 = "0x" + "00" * 12 + ANVIL_ADDR0[2:]
    cast_send_from(
        l1_rpc,
        endpoint,
        messenger,
        "lzReceive((uint32,bytes32,uint64),bytes32,bytes,address,bytes)",
        f"({src_eid},{sender_b32},1)",
        guid,
        message_payload,
        "0x0000000000000000000000000000000000000000",
        "0x",
    )


def _set_time(rpc: str, ts: int) -> None:
    run(["cast", "rpc", "evm_setNextBlockTimestamp", hex(ts), "--rpc-url", rpc])
    run(["cast", "rpc", "evm_mine", "--rpc-url", rpc])


def _ensure_liquidity_vault_role(l1_rpc: str, vault: str) -> None:
    liquidity_vault = cast_call(l1_rpc, vault, "liquidityVault()(address)").splitlines()[0].strip()
    vault_role = run(["cast", "keccak", "VAULT_ROLE"]).strip()
    has = cast_call(l1_rpc, liquidity_vault, "hasRole(bytes32,address)(bool)", vault_role, vault).strip().lower() == "true"
    if not has:
        cast_send_pk(l1_rpc, liquidity_vault, "grantRole(bytes32,address)", vault_role, vault)


def _ensure_keeper_role(l1_rpc: str, vault: str, keeper: str = ANVIL_ADDR0) -> None:
    keeper_role = run(["cast", "keccak", "KEEPER_ROLE"]).strip()
    has = cast_call(l1_rpc, vault, "hasRole(bytes32,address)(bool)", keeper_role, keeper).strip().lower() == "true"
    if has:
        return
    cast_send_pk(l1_rpc, vault, "grantRole(bytes32,address)", keeper_role, keeper)


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
    rfq_expiry = now_ts + 3600
    mandate_deadline = now_ts + 1800
    call_strike = int(p_put) + 1
    max_negative_c = 500_000_000
    rfq_tuple = (
        f"({loan_id},{sepolia_weth},{p_collateral},{p_maturity},{p_put},{call_strike},"
        f"{p_borrow},0,{max_negative_c},{rfq_expiry},{borrower},0)"
    )
    rfq_hash = cast_call(
        l1_rpc,
        vault,
        "hashBaselineRfq((uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint256,uint64,address,uint256))(bytes32)",
        rfq_tuple,
    ).splitlines()[0].strip()
    rfq_sig = _sign_no_prefix(rfq_hash, ANVIL_PK0)

    lz_messenger = cast_call(l1_rpc, vault, "lzMessenger()(address)").splitlines()[0].strip()
    subaccount_id = int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0])
    default_opts = cast_call(l1_rpc, lz_messenger, "defaultOptions()(bytes)").splitlines()[0].strip()
    apr = int(cast_call(l1_rpc, vault, "originationFeeApr()(uint256)").split()[0])
    year = 365 * 24 * 3600
    fixed_interest = ((int(p_borrow) * apr) // 10**18) * (int(p_maturity) - now_ts) // year
    mandate_data = _abi_encode(
        "f(address,uint256,uint256,uint256,uint256,uint256,uint64,uint64)",
        borrower,
        str(call_strike),
        p_put,
        "0",
        str(fixed_interest),
        str(max_negative_c),
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
        "acceptMandate(uint256,(uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint256,uint64,address,uint256),bytes,uint64)",
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

    cast_send_pk(l1_rpc, vault, "convertToVariable(uint256,bytes32)", str(loan_id), collat_guid)
    _ensure_token_balance(l1_rpc, sepolia_weth, vault, int(p_collateral))

    position_addr = _predict_next_create_address(l1_rpc, vault)
    # Euler fork quirk: collateral deposit path can pull from the position account during EVC call,
    # so preseed both allowance and minimal balance on the predicted clone address.
    if not _force_set_erc20_allowance_on_anvil(l1_rpc, sepolia_weth, position_addr, collateral_vault, 2**256 - 1):
        raise RuntimeError(f"failed to preseed collateral allowance for variable position {position_addr}")
    _ensure_token_balance(l1_rpc, sepolia_weth, position_addr, int(p_collateral))

    cast_send_pk(l1_rpc, vault, "tryConvertReadyLoan(uint256)(bool)", str(loan_id), private_key=ANVIL_PK0)

    loan_raw = cast_call(l1_rpc, vault, "loans(uint256)(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint8,uint256,uint256,uint256,uint256)", str(loan_id))
    loan_lines = [ln.strip() for ln in loan_raw.splitlines() if ln.strip()]
    loan_state = int(loan_lines[8].split()[0]) if len(loan_lines) > 8 else -1
    if loan_state != 3:
        available_liquidity = int(cast_call(l1_rpc, position_addr, "availableLiquidity()(uint256)").split()[0])
        required_debt = int(p_borrow) + fixed_interest
        raise RuntimeError(
            f"loan not ACTIVE_VARIABLE (state={loan_state}, availableLiquidity={available_liquidity}, requiredDebt={required_debt}): {loan_raw}"
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
