#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_run(script_name: str, chain_id: int) -> dict:
    run_path = ROOT / "broadcast" / script_name / str(chain_id) / "run-latest.json"
    if not run_path.is_file():
        raise FileNotFoundError(f"missing broadcast run file: {run_path}")
    return json.loads(run_path.read_text(encoding="utf-8"))


def _first_create_by_name(txs: list[dict], contract_name: str) -> str | None:
    for tx in txs:
        if tx.get("transactionType") == "CREATE" and tx.get("contractName") == contract_name:
            return tx.get("contractAddress")
    return None


def _creates_by_name(txs: list[dict], *contract_names: str) -> list[str]:
    names = set(contract_names)
    out: list[str] = []
    for tx in txs:
        if tx.get("transactionType") == "CREATE" and tx.get("contractName") in names:
            addr = tx.get("contractAddress")
            if addr:
                out.append(addr)
    return out


def _l1_from_run(run: dict) -> dict:
    txs: list[dict] = run.get("transactions", [])
    proxies = _creates_by_name(txs, "ERC1967Proxy", "TransparentUpgradeableProxy")
    out = {
        "l1Vault": proxies[0] if len(proxies) > 0 else None,
        "l1VaultProxy": proxies[0] if len(proxies) > 0 else None,
        "l1VaultImplementation": _first_create_by_name(txs, "CollarVault"),
        "l1Messenger": proxies[1] if len(proxies) > 1 else _first_create_by_name(txs, "CollarVaultMessenger"),
        "l1MessengerImplementation": _first_create_by_name(txs, "CollarVaultMessenger"),
        "l1FinalizeModule": _first_create_by_name(txs, "CollarVaultFinalizeModule"),
        "l1SettleModule": _first_create_by_name(txs, "CollarVaultSettleModule"),
        "l1RolloverModule": _first_create_by_name(txs, "CollarVaultRolloverModule"),
        "l1LiquidityVault": _first_create_by_name(txs, "CollarLiquidityVault"),
        "l1EulerAdapter": _first_create_by_name(txs, "EulerAdapterMock"),
        "l1WethAdapter": _first_create_by_name(txs, "SocketBridgeAdapterOld")
        or _first_create_by_name(txs, "SocketBridgeAdapterNew")
        or _first_create_by_name(txs, "SocketBridgeAdapter"),
    }
    return {k: v for k, v in out.items() if v}


def _l2_from_run(run: dict) -> dict:
    txs: list[dict] = run.get("transactions", [])
    proxies = _creates_by_name(txs, "ERC1967Proxy", "TransparentUpgradeableProxy")
    out = {
        "l2Receiver": proxies[1] if len(proxies) > 1 else _first_create_by_name(txs, "CollarTSAReceiver"),
        "l2ReceiverImplementation": _first_create_by_name(txs, "CollarTSAReceiver"),
        "l2SocketTracker": _first_create_by_name(txs, "SocketMessageTrackerMock"),
        "l2LoanStore": _first_create_by_name(txs, "CollarLoanStore"),
        "l2Tsa": proxies[0] if len(proxies) > 0 else None,
        "l2TsaImplementation": _first_create_by_name(txs, "CollarTSA"),
        "l2OptionRiskVerifier": _first_create_by_name(txs, "OptionRiskVerifier"),
        "l2RfqVerifier": _first_create_by_name(txs, "RfqVerifier"),
        "l2LzEndpoint": _first_create_by_name(txs, "LZEndpointV2Mock"),
    }
    return {k: v for k, v in out.items() if v}


def main() -> None:
    parser = argparse.ArgumentParser(description="Export deployment addresses from Foundry broadcast logs")
    parser.add_argument("chain_id", type=int)
    parser.add_argument("layer", choices=["l1", "l2"])
    args = parser.parse_args()

    script_name = "DeployL1.s.sol" if args.layer == "l1" else "DeployL2.s.sol"
    run = _load_run(script_name, args.chain_id)
    addrs = _l1_from_run(run) if args.layer == "l1" else _l2_from_run(run)

    out_dir = ROOT / "deployments" / str(args.chain_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.layer}.json"
    out_path.write_text(json.dumps(addrs, indent=2) + "\n", encoding="utf-8")

    print(out_path)


if __name__ == "__main__":
    main()
