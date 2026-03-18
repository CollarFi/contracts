#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]

ANVIL_PK0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDR0 = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
BORROWER_PK = "0x59c6995e998f97a5a0044966f0945382d77ad9e6f3c6f7f8b8d7a0f4f7f9d6f1"
SEED_USDC_HOLDER = "0xDf4fF02E2dDe3A08590829d7398Cc31B0255bAb5"
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


def print_step(ok: bool, text: str) -> None:
    print(("✅" if ok else "❌") + " " + text)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def load_addrs(path: Path) -> dict:
    data = load_json(path)
    return data.get("addrs", data)


def _extract_addresses(raw: str, expected: int, label: str) -> list[str]:
    addrs = re.findall(r"0x[a-fA-F0-9]{40}", raw)
    if len(addrs) < expected:
        raise RuntimeError(f"failed to parse {label}: {raw}")
    return addrs[:expected]


def resolve_l2_runtime_env(l2_rpc: str, l2_addrs: dict, receiver: str | None = None) -> dict[str, str]:
    tsa = str(l2_addrs["l2Tsa"])
    receiver_addr = receiver or str(l2_addrs["l2Receiver"])
    atomic_executor = str(l2_addrs["l2AtomicExecutor"])

    collar_addrs = _extract_addresses(
        cast_call(
            l2_rpc,
            tsa,
            "getCollarTSAAddresses()(address,address,address,address,address,address)",
        ),
        6,
        "getCollarTSAAddresses",
    )
    base_addrs = _extract_addresses(
        cast_call(
            l2_rpc,
            tsa,
            "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
        ),
        7,
        "getBaseTSAAddresses",
    )

    return {
        "RPC_URL": l2_rpc,
        "L2_RECEIVER": receiver_addr,
        "L2_TSA": tsa,
        "ATOMIC_EXECUTOR": atomic_executor,
        "BASE_FEED": collar_addrs[0],
        "DEPOSIT_MODULE": collar_addrs[1],
        "WITHDRAWAL_MODULE": collar_addrs[2],
        "TRADE_MODULE": collar_addrs[3],
        "RFQ_MODULE": collar_addrs[4],
        "OPTION_ASSET": collar_addrs[5],
        "WRAPPED_DEPOSIT_ASSET": base_addrs[2],
        "MATCHING": base_addrs[6],
    }


def write_env_with_updates(base_env_path: Path, out_path: Path, updates: dict[str, str]) -> Path:
    lines = [base_env_path.read_text().rstrip("\n")]
    for key, value in updates.items():
        lines.append(f"{key}={value}")
    out_path.write_text("\n".join(lines) + "\n")
    return out_path


def extract_local_port(rpc: str) -> int | None:
    try:
        parsed = urlparse(rpc)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        return None
    return parsed.port


def deployment_artifact_issues(
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
            l1 = load_addrs(l1_path)
        except Exception as e:
            issues.append(f"failed reading {l1_path}: {e}")

    if not l2_path.exists():
        issues.append(f"missing artifact: {l2_path}")
    else:
        try:
            l2 = load_addrs(l2_path)
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
        "l2AtomicExecutor": "L2 atomic executor",
    }

    if l1 is not None:
        for key, label in required_l1.items():
            addr = l1.get(key, "")
            if not isinstance(addr, str) or not ADDR_RE.match(addr):
                issues.append(f"invalid {key} in {l1_path}: {addr}")
                continue
            if not has_code(l1_rpc, addr):
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
            if not has_code(l2_rpc, addr):
                issues.append(f"no runtime code for {label} ({addr}) on {l2_rpc}")

    return issues, l1, l2


def run_deployment_e2e(
    l1_port: int,
    l2_port: int,
    sepolia_usdc: str,
    sepolia_weth: str,
    weth_socket_vault: str,
    weth_socket_connector: str,
) -> None:
    run([
        "uv", "run", "python", str(ROOT / "test/e2e/deployment_e2e.py"),
        "--l1-port", str(l1_port),
        "--l2-port", str(l2_port),
        "--l1-usdc-asset", sepolia_usdc,
        "--l1-weth-asset", sepolia_weth,
        "--weth-socket-vault", weth_socket_vault,
        "--weth-socket-connector", weth_socket_connector,
        "--keep-anvil",
    ])


def ensure_live_deployments(
    l1_path: Path,
    l2_path: Path,
    l1_rpc: str,
    l2_rpc: str,
    sepolia_usdc: str,
    sepolia_weth: str,
    auto_redeploy: bool,
    weth_socket_vault: str,
    weth_socket_connector: str,
) -> tuple[dict, dict, bool]:
    issues, l1, l2 = deployment_artifact_issues(l1_path, l2_path, l1_rpc, l2_rpc, sepolia_usdc)
    if not issues:
        return l1 or {}, l2 or {}, False

    if not auto_redeploy:
        raise RuntimeError("deployment artifacts are stale/invalid:\n- " + "\n- ".join(issues))

    l1_port = extract_local_port(l1_rpc)
    l2_port = extract_local_port(l2_rpc)
    if l1_port is None or l2_port is None:
        raise RuntimeError(
            "deployment artifacts are stale/invalid and auto-redeploy requires local RPC URLs "
            f"(got l1={l1_rpc}, l2={l2_rpc}):\n- " + "\n- ".join(issues)
        )

    run_deployment_e2e(l1_port, l2_port, sepolia_usdc, sepolia_weth, weth_socket_vault, weth_socket_connector)

    issues2, l1_after, l2_after = deployment_artifact_issues(l1_path, l2_path, l1_rpc, l2_rpc, sepolia_usdc)
    if issues2:
        raise RuntimeError("deployment_e2e completed but artifacts/runtime still invalid:\n- " + "\n- ".join(issues2))
    return l1_after or {}, l2_after or {}, True


def extract_address_from_forge_create(out: str) -> str:
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


def ensure_l1_sepolia_rpc(rpc: str) -> None:
    got = int(run(["cast", "chain-id", "--rpc-url", rpc]).strip())
    if got != 11155111:
        raise RuntimeError(f"expected L1 Sepolia fork (11155111), got {got}")


def ensure_l2_derive_rpc(rpc: str) -> None:
    got = int(run(["cast", "chain-id", "--rpc-url", rpc]).strip())
    if got != 901:
        raise RuntimeError(f"expected L2 Derive fork (901), got {got}")


def set_eth_balance(rpc: str, who: str, wei_hex: str = "0x3635C9ADC5DEA00000") -> None:
    run(["cast", "rpc", "anvil_setBalance", who, wei_hex, "--rpc-url", rpc])


def has_code(rpc: str, addr: str) -> bool:
    return run(["cast", "code", addr, "--rpc-url", rpc]).strip().lower() != "0x"


def require_code(rpc: str, addr: str, label: str) -> None:
    if not has_code(rpc, addr):
        raise RuntimeError(
            f"missing code for {label} at {addr} on {rpc}; use a Sepolia fork source with required deployments"
        )


def deploy_contract(rpc: str, contract: str, *ctor_args: str) -> str:
    cmd = [
        "forge", "create", contract,
        "--rpc-url", rpc,
        "--private-key", ANVIL_PK0,
        "--broadcast",
    ]
    if ctor_args:
        cmd += ["--constructor-args", *ctor_args]
    out = run(cmd)
    return extract_address_from_forge_create(out)


def keccak_hex(hex_data: str) -> str:
    return run(["cast", "keccak", hex_data]).splitlines()[0].strip()


def abi_encode(sig: str, *args: str) -> str:
    return run(["cast", "abi-encode", sig, *args]).strip()


def force_set_erc20_balance_on_anvil(rpc: str, token: str, who: str, target_amount: int) -> bool:
    for slot in range(0, 128):
        key = keccak_hex(abi_encode("f(address,uint256)", who, str(slot)))
        run([
            "cast", "rpc", "anvil_setStorageAt", token, key, run(["cast", "to-bytes32", str(target_amount)]), "--rpc-url", rpc,
        ])
        bal = int(cast_call(rpc, token, "balanceOf(address)(uint256)", who).split()[0])
        if bal >= target_amount:
            return True
    return False


def force_set_erc20_allowance_on_anvil(rpc: str, token: str, owner: str, spender: str, target_amount: int) -> bool:
    target_b32 = "0x" + int(target_amount).to_bytes(32, "big", signed=False).hex()
    for slot in range(0, 256):
        owner_slot = keccak_hex(abi_encode("f(address,uint256)", owner, str(slot)))
        key = keccak_hex(abi_encode("f(address,bytes32)", spender, owner_slot))
        run(["cast", "rpc", "anvil_setStorageAt", token, key, target_b32, "--rpc-url", rpc])
        allowance = int(cast_call(rpc, token, "allowance(address,address)(uint256)", owner, spender).split()[0])
        if allowance >= target_amount:
            return True
    return False


def seed_usdc_liquidity(rpc: str, usdc: str, debt_vault: str, amount: int) -> None:
    if amount <= 0:
        return

    cash_before = int(cast_call(rpc, debt_vault, "cash()(uint256)").split()[0])
    target_cash = cash_before + amount

    if force_set_erc20_balance_on_anvil(rpc, usdc, ANVIL_ADDR0, amount):
        cast_send_pk(rpc, usdc, "approve(address,uint256)", debt_vault, str(amount), private_key=ANVIL_PK0)
        cast_send_pk(rpc, debt_vault, "deposit(uint256,address)", str(amount), ANVIL_ADDR0, private_key=ANVIL_PK0)
    else:
        set_eth_balance(rpc, SEED_USDC_HOLDER)
        cast_send_from(rpc, SEED_USDC_HOLDER, usdc, "approve(address,uint256)", debt_vault, str(amount))
        cast_send_from(rpc, SEED_USDC_HOLDER, debt_vault, "deposit(uint256,address)", str(amount), ANVIL_ADDR0)

    cash_after = int(cast_call(rpc, debt_vault, "cash()(uint256)").split()[0])
    if cash_after < target_cash:
        # Fallback: force-fund, transfer underlying, then `skim` to realize vault cash.
        ensure_token_balance(rpc, usdc, ANVIL_ADDR0, amount)
        cast_send_pk(rpc, usdc, "transfer(address,uint256)", debt_vault, str(amount), private_key=ANVIL_PK0)
        try:
            deficit = max(0, target_cash - cash_after)
            if deficit > 0:
                cast_send_pk(rpc, debt_vault, "skim(uint256,address)", str(deficit), ANVIL_ADDR0, private_key=ANVIL_PK0)
        except Exception:
            # Some vault/token combinations may not require or permit skim in this path.
            pass
        cash_after = int(cast_call(rpc, debt_vault, "cash()(uint256)").split()[0])

    if cash_after < target_cash:
        raise RuntimeError(
            f"failed to seed Euler debt vault cash: target={target_cash}, actual={cash_after}, debtVault={debt_vault}, usdc={usdc}"
        )


def seed_l1_liquidity_vault(rpc: str, usdc: str, liquidity_vault: str, amount: int) -> None:
    if amount <= 0:
        return
    if force_set_erc20_balance_on_anvil(rpc, usdc, ANVIL_ADDR0, amount):
        try:
            cast_send_pk(rpc, usdc, "approve(address,uint256)", liquidity_vault, str(amount), private_key=ANVIL_PK0)
            allowance = int(
                cast_call(rpc, usdc, "allowance(address,address)(uint256)", ANVIL_ADDR0, liquidity_vault).split()[0]
            )
            if allowance < amount:
                force_set_erc20_allowance_on_anvil(rpc, usdc, ANVIL_ADDR0, liquidity_vault, amount)
            cast_send_pk(
                rpc,
                liquidity_vault,
                "deposit(uint256,address)",
                str(amount),
                ANVIL_ADDR0,
                private_key=ANVIL_PK0,
            )
            return
        except Exception:
            # Some fork/token states do not cooperate with the locally-forced balance path.
            # Fall back to the live-holder route below instead of failing the entire scenario.
            pass

    set_eth_balance(rpc, SEED_USDC_HOLDER)
    cast_send_from(rpc, SEED_USDC_HOLDER, usdc, "approve(address,uint256)", liquidity_vault, str(amount))
    cast_send_from(rpc, SEED_USDC_HOLDER, liquidity_vault, "deposit(uint256,address)", str(amount), ANVIL_ADDR0)


def ensure_token_balance(rpc: str, token: str, who: str, amount: int) -> None:
    current = int(cast_call(rpc, token, "balanceOf(address)(uint256)", who).split()[0])
    if current >= amount:
        return
    if not force_set_erc20_balance_on_anvil(rpc, token, who, amount):
        raise RuntimeError(f"failed to top up {token} for {who}: {current} < {amount}")


def run_fresh_loan_flow(
    l1_json: Path,
    l2_json: Path,
    l1_rpc: str,
    l2_rpc: str,
    collateral_asset: str,
    *,
    relay_l2_ack_to_l1: bool = True,
) -> dict:
    cmd = [
        "uv", "run", "python", str(ROOT / "test/e2e/fresh_loan_flow.py"),
        "--l1-json", str(l1_json),
        "--l2-json", str(l2_json),
        "--l1-rpc", l1_rpc,
        "--l2-rpc", l2_rpc,
        "--collateral-asset", collateral_asset,
        "--json",
    ]
    if not relay_l2_ack_to_l1:
        cmd.append("--no-relay-l2-ack-to-l1")
    out = run(cmd)
    return json.loads(out)


def borrower_address() -> str:
    return run(["cast", "wallet", "address", "--private-key", BORROWER_PK]).strip()


def predict_next_create_address(rpc: str, deployer: str) -> str:
    nonce = int(run(["cast", "nonce", deployer, "--rpc-url", rpc]).split()[0])
    out = run(["cast", "compute-address", deployer, "--nonce", str(nonce)])
    m = re.search(r"0x[a-fA-F0-9]{40}", out)
    if not m:
        raise RuntimeError(f"failed to parse compute-address output: {out}")
    return m.group(0)


def sign_no_prefix(digest_hex: str, private_key: str) -> str:
    return run(["cast", "wallet", "sign", "--no-hash", "--private-key", private_key, digest_hex]).strip()


def inject_lz_message(l1_rpc: str, messenger: str, guid: str, message_payload: str) -> None:
    endpoint = cast_call(l1_rpc, messenger, "endpoint()(address)").splitlines()[0].strip()
    set_eth_balance(l1_rpc, endpoint)
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


def set_time(rpc: str, ts: int) -> None:
    run(["cast", "rpc", "evm_setNextBlockTimestamp", hex(ts), "--rpc-url", rpc])
    run(["cast", "rpc", "evm_mine", "--rpc-url", rpc])


def ensure_liquidity_vault_role(l1_rpc: str, vault: str) -> None:
    liquidity_vault = cast_call(l1_rpc, vault, "liquidityVault()(address)").splitlines()[0].strip()
    vault_role = run(["cast", "keccak", "VAULT_ROLE"]).strip()
    has = cast_call(l1_rpc, liquidity_vault, "hasRole(bytes32,address)(bool)", vault_role, vault).strip().lower() == "true"
    if not has:
        cast_send_pk(l1_rpc, liquidity_vault, "grantRole(bytes32,address)", vault_role, vault)


def ensure_keeper_role(l1_rpc: str, vault: str, keeper: str = ANVIL_ADDR0) -> None:
    keeper_role = run(["cast", "keccak", "KEEPER_ROLE"]).strip()
    has = cast_call(l1_rpc, vault, "hasRole(bytes32,address)(bool)", keeper_role, keeper).strip().lower() == "true"
    if has:
        return
    cast_send_pk(l1_rpc, vault, "grantRole(bytes32,address)", keeper_role, keeper)
