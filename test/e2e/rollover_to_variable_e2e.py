#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[2]
ANVIL_PK0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDR0 = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
SEED_USDC_HOLDER = "0xDf4fF02E2dDe3A08590829d7398Cc31B0255bAb5"
ORACLE_PRICE_SCALE = 10**36
WETH_PRICE_USD = 3000 * 10**18
USDC_PRICE_USD = 1 * 10**30

app = typer.Typer(add_completion=False)


def run(cmd: list[str]) -> str:
    import subprocess

    p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout.strip()


def cast_call(rpc: str, to: str, sig: str, *args: str) -> str:
    return run(["cast", "call", to, sig, *args, "--rpc-url", rpc]).strip()


def cast_send_pk(rpc: str, to: str, sig: str, *args: str, private_key: str = ANVIL_PK0) -> str:
    return run(["cast", "send", to, sig, *args, "--rpc-url", rpc, "--private-key", private_key])


def cast_send_from(rpc: str, frm: str, to: str, sig: str, *args: str) -> str:
    return run(["cast", "send", to, sig, *args, "--rpc-url", rpc, "--unlocked", "--from", frm])


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


def _deploy_fixed_oracle(rpc: str, price: int) -> str:
    out = run([
        "forge",
        "create",
        "src/mocks/FixedPriceOracleMock.sol:FixedPriceOracleMock",
        "--rpc-url",
        rpc,
        "--private-key",
        ANVIL_PK0,
        "--broadcast",
        "--constructor-args",
        str(price),
    ])
    return _extract_address_from_forge_create(out)


def _create_evault(rpc: str, factory: str, implementation: str, asset: str, oracle: str, unit_of_account: str) -> str:
    trailing = "0x" + asset[2:] + oracle[2:] + unit_of_account[2:]
    cast_send_pk(rpc, factory, "createProxy(address,bool,bytes)(address)", implementation, "true", trailing)
    length = int(cast_call(rpc, factory, "getProxyListLength()(uint256)").split()[0])
    return cast_call(rpc, factory, "proxyList(uint256)(address)", str(length - 1)).splitlines()[0].strip()


def _configure_vault_basics(rpc: str, vault: str) -> None:
    # ensure hooks are disabled for plain deposit/borrow paths in this synthetic E2E setup
    cast_send_pk(rpc, vault, "setHookConfig(address,uint32)", "0x0000000000000000000000000000000000000000", "0")
    # no caps for this synthetic e2e setup
    cast_send_pk(rpc, vault, "setCaps(uint16,uint16)", "0", "0")


def _set_ltv(rpc: str, debt_vault: str, collateral_vault: str, borrow_ltv_bps: int = 8500, liq_ltv_bps: int = 9000) -> None:
    cast_send_pk(
        rpc,
        debt_vault,
        "setLTV(address,uint16,uint16,uint32)",
        collateral_vault,
        str(borrow_ltv_bps),
        str(liq_ltv_bps),
        "0",
    )


def _seed_usdc_liquidity(rpc: str, usdc: str, debt_vault: str, amount: int) -> None:
    _set_eth_balance(rpc, SEED_USDC_HOLDER)
    cast_send_from(rpc, SEED_USDC_HOLDER, usdc, "approve(address,uint256)", debt_vault, str(amount))
    cast_send_from(rpc, SEED_USDC_HOLDER, debt_vault, "deposit(uint256,address)", str(amount), ANVIL_ADDR0)


@app.command()
def main(
    l1_json: Path = typer.Option(Path("deployments/11155111/l1-e2e.json")),
    l2_json: Path = typer.Option(Path("deployments/901/l2-e2e.json")),
    l1_rpc: str = typer.Option("http://127.0.0.1:10111"),
    l2_rpc: str = typer.Option("http://127.0.0.1:10019"),
    sepolia_usdc: str = typer.Option("0x565810cbfa3cf1390963e5afa2fb953795686339"),
    sepolia_weth: str = typer.Option("0xe67abda0d43f7ac8f37876bbf00d1dfadbb93aaa"),
    usdc_seed: int = typer.Option(200_000 * 10**6),
):
    print("=== collar.fi rollover-to-variable e2e ===")

    _ensure_l1_sepolia_rpc(l1_rpc)
    _ensure_l2_derive_rpc(l2_rpc)
    _print_step(True, "RPC topology verified (L1=11155111, L2=901)")

    core = _load_euler_core_addresses(11155111)
    evc = core["evc"]
    factory = core["eVaultFactory"]
    implementation = core["eVaultImplementation"]
    _print_step(True, f"Euler core loaded (evc={evc}, factory={factory})")

    l1_path = ROOT / l1_json
    l2_path = ROOT / l2_json
    if not l1_path.exists() or not l2_path.exists():
        _print_step(False, "Missing deployment artifacts for Sepolia/Derive pairing")
        raise SystemExit(1)

    l1 = _load_json(l1_path).get("addrs", _load_json(l1_path))
    l2 = _load_json(l2_path).get("addrs", _load_json(l2_path))
    _print_step(True, f"Loaded deployments: L1 vault={l1.get('l1Vault')} L2 receiver={l2.get('l2Receiver')}")

    # 1) Deploy fixed-price oracles for synthetic test markets.
    weth_oracle = _deploy_fixed_oracle(l1_rpc, WETH_PRICE_USD)
    usdc_oracle = _deploy_fixed_oracle(l1_rpc, USDC_PRICE_USD)
    _print_step(True, f"Deployed fixed oracles (WETH={weth_oracle}, USDC={usdc_oracle})")

    # 2) Create EVaults for requested assets with hardcoded-price oracles.
    collateral_vault = _create_evault(l1_rpc, factory, implementation, sepolia_weth, weth_oracle, sepolia_usdc)
    debt_vault = _create_evault(l1_rpc, factory, implementation, sepolia_usdc, usdc_oracle, sepolia_usdc)
    _print_step(True, f"Created EVaults (collateral={collateral_vault}, debt={debt_vault})")

    _configure_vault_basics(l1_rpc, collateral_vault)
    _configure_vault_basics(l1_rpc, debt_vault)
    _print_step(True, "Configured EVault basics (hooks off, caps open)")

    # 3) Allow borrowing USDC against WETH at 85% LTV.
    _set_ltv(l1_rpc, debt_vault, collateral_vault, 8500, 9000)
    _print_step(True, "Configured debt EVault LTV (borrow=85%, liquidation=90%)")

    # 4) Seed debt-vault liquidity from provided holder.
    _seed_usdc_liquidity(l1_rpc, sepolia_usdc, debt_vault, usdc_seed)
    _print_step(True, f"Seeded USDC liquidity in debt EVault ({usdc_seed} units)")

    # TODO(next): wire adapter + run neutral rollover/conversion assertions.
    out = {
        "status": "evault-bootstrap-complete",
        "l1Vault": l1.get("l1Vault"),
        "l2Receiver": l2.get("l2Receiver"),
        "evc": evc,
        "collateralVault": collateral_vault,
        "debtVault": debt_vault,
        "wethOracle": weth_oracle,
        "usdcOracle": usdc_oracle,
    }
    p = Path(tempfile.mkdtemp(prefix="rollover-var-e2e-")) / "bootstrap.json"
    p.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Bootstrap artifact written: {p}")

    print("\nResult: PARTIAL_SUCCESS (EVault bootstrap complete)")


if __name__ == "__main__":
    app()


if __name__ == "__main__":
    app()
