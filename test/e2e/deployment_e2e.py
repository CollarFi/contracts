#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

import typer

import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR / "ops"))
sys.path.insert(0, str(THIS_DIR))
from lz_harness.common import load_env, run  # noqa: E402
from py_lib.lz import encode_lz_receive_option  # noqa: E402
from defaults import (  # noqa: E402
    L1_ANVIL_PORT,
    L1_ARTIFACT_JSON,
    L1_CHAIN_ID,
    L1_COLLATERAL_ASSET,
    L1_DEBT_ASSET,
    L1_WETH_SOCKET_CONNECTOR,
    L1_WETH_SOCKET_VAULT,
    L2_ANVIL_PORT,
    L2_ARTIFACT_JSON,
    L2_CHAIN_ID,
)

app = typer.Typer(add_completion=False)


def _status_mark(ok: bool) -> str:
    return "✅" if ok else "⚠️"


def _print_human_report(report: dict) -> None:
    print("\n=== collar.fi fresh deployment e2e ===")
    print(f"L1 fork env: {report['l1ForkEnv']}")
    print(f"L2 fork env: {report['l2ForkEnv']}")
    print(f"L1 artifacts: {report['l1OutputJson']}")
    print(f"L2 artifacts: {report['l2OutputJson']}")

    l1 = report.get("l1Addrs", {})
    l2 = report.get("l2Addrs", {})
    print("\nDeployed contracts")
    print(f"- L1 vault: {l1.get('l1Vault', 'n/a')}")
    print(f"- L1 messenger: {l1.get('l1Messenger', 'n/a')}")
    print(f"- L2 receiver: {l2.get('l2Receiver', 'n/a')}")
    print(f"- L2 TSA: {l2.get('l2Tsa', 'n/a')}")

    l2_keeper = report.get("l2Keeper", {}) if isinstance(report.get("l2Keeper"), dict) else {}
    l1_keeper = report.get("l1Keeper", {}) if isinstance(report.get("l1Keeper"), dict) else {}
    l2_msgs = report.get("l2Messages", {}) if isinstance(report.get("l2Messages"), dict) else {}
    l1_msgs = report.get("l1Messages", {}) if isinstance(report.get("l1Messages"), dict) else {}

    l2_handled = len(l2_keeper.get("handled", []))
    l1_handled = len(l1_keeper.get("handled", []))
    l2_results = len(l2_msgs.get("results", []))
    l1_results = len(l1_msgs.get("results", []))

    print("\nPost-deploy checks")
    print(f"- {_status_mark(True)} L2 keeper run completed ({l2_handled} handled messages)")
    print(f"- {_status_mark(True)} L1 keeper run completed ({l1_handled} handled messages)")
    print(f"- {_status_mark(True)} L2 message preflight completed ({l2_results} rows)")
    print(f"- {_status_mark(True)} L1 message preflight completed ({l1_results} rows)")
    print("\nDone.")

ANVIL_PK0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDR0 = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"


def _read_addrs(path: Path) -> dict:
    data = json.loads(path.read_text())
    return data.get("addrs", data)


def _spawn_anvil(rpc_url: str, port: int, chain_id: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "anvil",
            "--fork-url",
            rpc_url,
            "--port",
            str(port),
            "--chain-id",
            str(chain_id),
            "--auto-impersonate",
            "--silent",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write_env(base: dict[str, str], out: Path, rpc_url: str, updates: dict[str, str]) -> None:
    merged = dict(base)
    merged["RPC_URL"] = rpc_url
    merged.update(updates)
    lines = [f"{k}={v}" for k, v in merged.items()]
    out.write_text("\n".join(lines) + "\n")


def _cast_send(rpc: str, frm: str, to: str, sig: str, *args: str) -> str:
    return run(["cast", "send", to, sig, *args, "--rpc-url", rpc, "--unlocked", "--from", frm])


def _cast_send_pk(rpc: str, to: str, sig: str, *args: str, private_key: str = ANVIL_PK0) -> str:
    return run(["cast", "send", to, sig, *args, "--rpc-url", rpc, "--private-key", private_key])


def _loads_json_relaxed(raw: str) -> dict:
    return json.loads(raw, strict=False)


def _peer_bytes32(addr: str) -> str:
    return "0x" + "00" * 12 + addr.lower().removeprefix("0x")


def _grant_role_if_needed(rpc: str, contract: str, role: str, account: str, admin: str) -> None:
    has = run(["cast", "call", contract, "hasRole(bytes32,address)(bool)", role, account, "--rpc-url", rpc]).strip().lower()
    if has != "true":
        _cast_send(rpc, admin, contract, "grantRole(bytes32,address)", role, account)


def _find_default_admin(rpc: str, contract: str, candidates: list[str]) -> str:
    admin_role = "0x" + "00" * 32
    for c in candidates:
        if not c:
            continue
        try:
            ok = run(["cast", "call", contract, "hasRole(bytes32,address)(bool)", admin_role, c, "--rpc-url", rpc]).strip().lower()
            if ok == "true":
                return c
        except Exception:
            pass
    raise RuntimeError(f"could not find DEFAULT_ADMIN_ROLE holder for {contract}")


def _wait_for_chain_id(rpc_url: str, expected_chain_id: int, timeout_s: int, poll_s: float) -> None:
    deadline = time.time() + timeout_s
    last_err = ""
    while time.time() < deadline:
        try:
            got = run(["cast", "chain-id", "--rpc-url", rpc_url]).strip()
            if int(got) == expected_chain_id:
                return
            last_err = f"chain id mismatch: got {got}, want {expected_chain_id}"
        except Exception as exc:
            last_err = str(exc)
        time.sleep(poll_s)
    raise RuntimeError(f"rpc not ready at {rpc_url} within {timeout_s}s ({last_err})")


@app.command()
def main(
    l1_env: Path = typer.Option(ROOT_DIR / ".env.l1.testnet"),
    l2_env: Path = typer.Option(ROOT_DIR / ".env.l2.testnet"),
    l1_port: int = typer.Option(L1_ANVIL_PORT),
    l2_port: int = typer.Option(L2_ANVIL_PORT),
    l1_chain_id: int = typer.Option(L1_CHAIN_ID),
    l2_chain_id: int = typer.Option(L2_CHAIN_ID),
    l1_usdc_asset: str = typer.Option(L1_DEBT_ASSET, help="Override L1 USDC_ASSET for deploy env"),
    l1_weth_asset: str = typer.Option(L1_COLLATERAL_ASSET, help="Override L1 WETH_ASSET for deploy env"),
    weth_socket_vault: str = typer.Option(L1_WETH_SOCKET_VAULT, help="Override WETH_SOCKET_VAULT for fork deploy env"),
    weth_socket_connector: str = typer.Option(L1_WETH_SOCKET_CONNECTOR, help="Override WETH_SOCKET_CONNECTOR for fork deploy env"),
    disable_weth_socket_adapter: bool = typer.Option(False, help="Clear WETH socket adapter envs for fork deploy"),
    derive_registry_profile: str = typer.Option("testnet"),
    anvil_ready_timeout_s: int = typer.Option(30, help="Timeout waiting for fork RPC readiness"),
    anvil_ready_poll_s: float = typer.Option(0.5, help="Polling interval while waiting for fork RPC"),
    keep_anvil: bool = typer.Option(False, help="Keep anvil processes running"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON report"),
) -> None:
    l1e = load_env(l1_env)
    l2e = load_env(l2_env)

    p1 = _spawn_anvil(l1e["RPC_URL"], l1_port, l1_chain_id)
    p2 = _spawn_anvil(l2e["RPC_URL"], l2_port, l2_chain_id)

    l1_rpc = f"http://127.0.0.1:{l1_port}"
    l2_rpc = f"http://127.0.0.1:{l2_port}"

    _wait_for_chain_id(l1_rpc, l1_chain_id, anvil_ready_timeout_s, anvil_ready_poll_s)
    _wait_for_chain_id(l2_rpc, l2_chain_id, anvil_ready_timeout_s, anvil_ready_poll_s)

    tmpdir = Path(tempfile.mkdtemp(prefix="collar-e2e-"))
    l1_fork_env = tmpdir / "l1.fork.env"
    l2_fork_env = tmpdir / "l2.fork.env"

    l1_out = ROOT_DIR / "deployments" / str(l1_chain_id) / L1_ARTIFACT_JSON.name
    l2_out = ROOT_DIR / "deployments" / str(l2_chain_id) / L2_ARTIFACT_JSON.name

    l1_eid = ""
    if l1e.get("LZ_ENDPOINT"):
        try:
            l1_eid = run(["cast", "call", l1e["LZ_ENDPOINT"], "eid()(uint32)", "--rpc-url", l1_rpc]).split()[0]
        except Exception:
            l1_eid = ""

    l2_eid = ""
    if l2e.get("LZ_ENDPOINT"):
        try:
            l2_eid = run(["cast", "call", l2e["LZ_ENDPOINT"], "eid()(uint32)", "--rpc-url", l2_rpc]).split()[0]
        except Exception:
            l2_eid = ""
    l1_out.parent.mkdir(parents=True, exist_ok=True)
    l2_out.parent.mkdir(parents=True, exist_ok=True)

    # Deploy fresh L2 first.
    _write_env(
        l2e,
        l2_fork_env,
        l2_rpc,
        {
            "ACCOUNT": "CDPDeployer",
            "OUTPUT_JSON": str(l2_out.relative_to(ROOT_DIR)),
            # Force fresh local components in fork E2E; avoid ambient env contamination
            # that could point to privileged external contracts.
            "LOAN_STORE": "0x0000000000000000000000000000000000000000",
            "TSA_PROXY": "0x0000000000000000000000000000000000000000",
            "TSA_IMPLEMENTATION": "0x0000000000000000000000000000000000000000",
            "OPTION_RISK_VERIFIER": "0x0000000000000000000000000000000000000000",
            "RFQ_VERIFIER": "0x0000000000000000000000000000000000000000",
            "RFQ_DELEGATE_MODULE": "0x0000000000000000000000000000000000000000",
            "L1_EID": l1_eid,
        },
    )
    run(
        [
            sys.executable,
            str(ROOT_DIR / "ops/deploy_l2.py"),
            str(l2_fork_env),
            "--broadcast",
            "--private-key",
            ANVIL_PK0,
            "--derive-registry-profile",
            derive_registry_profile,
            "--json",
        ]
    )

    # Deploy fresh L1 wired to the new L2.
    l1_updates = {
        "ACCOUNT": "CDPDeployer",
        "OUTPUT_JSON": str(l1_out.relative_to(ROOT_DIR)),
        "L2_EID": l2_eid,
    }
    if l1_usdc_asset:
        l1_updates["USDC_ASSET"] = l1_usdc_asset
    if l1_weth_asset:
        l1_updates["WETH_ASSET"] = l1_weth_asset
    if disable_weth_socket_adapter:
        l1_updates["WETH_SOCKET_VAULT"] = "0x0000000000000000000000000000000000000000"
        l1_updates["WETH_SOCKET_BRIDGE"] = "0x0000000000000000000000000000000000000000"
        l1_updates["WETH_SOCKET_CONNECTOR"] = "0x0000000000000000000000000000000000000000"
    else:
        # Force old Socket adapter path in fork e2e unless explicitly disabled.
        # Keep new-bridge mode off to avoid env contamination from base .env files.
        l1_updates["WETH_SOCKET_BRIDGE"] = "0x0000000000000000000000000000000000000000"
        if weth_socket_vault:
            l1_updates["WETH_SOCKET_VAULT"] = weth_socket_vault
        if weth_socket_connector:
            l1_updates["WETH_SOCKET_CONNECTOR"] = weth_socket_connector

    _write_env(l1e, l1_fork_env, l1_rpc, l1_updates)
    run(
        [
            sys.executable,
            str(ROOT_DIR / "ops/deploy_l1.py"),
            str(l1_fork_env),
            "--l2-env-file",
            str(l2_fork_env),
            "--broadcast",
            "--private-key",
            ANVIL_PK0,
            "--json",
        ]
    )

    l1a = _read_addrs(l1_out)
    l2a = _read_addrs(l2_out)

    # Ensure LZ peer wiring + default options are set for fresh fork deploys.
    l1_messenger = l1a["l1Messenger"]
    l2_receiver_addr = l2a["l2Receiver"]
    if l2_eid:
        _cast_send_pk(l1_rpc, l1_messenger, "setPeer(uint32,bytes32)", l2_eid, _peer_bytes32(l2_receiver_addr))
    if l1_eid:
        _cast_send_pk(l2_rpc, l2_receiver_addr, "setPeer(uint32,bytes32)", l1_eid, _peer_bytes32(l1_messenger))

    receive_gas = int(l1e.get("LZ_RECEIVE_GAS") or l2e.get("LZ_RECEIVE_GAS") or "200000")
    receive_value = int(l1e.get("LZ_RECEIVE_VALUE") or l2e.get("LZ_RECEIVE_VALUE") or "0")
    default_options = encode_lz_receive_option(receive_gas, receive_value)
    _cast_send_pk(l1_rpc, l1_messenger, "setDefaultOptions(bytes)", default_options)
    _cast_send_pk(l2_rpc, l2_receiver_addr, "setDefaultOptions(bytes)", default_options)

    # refresh envs with concrete deployed addresses
    _write_env(
        l1e,
        l1_fork_env,
        l1_rpc,
        {
            "L1_VAULT": l1a.get("l1Vault", ""),
            "L1_MESSENGER": l1a.get("l1Messenger", ""),
            "OUTPUT_JSON": str(l1_out.relative_to(ROOT_DIR)),
            "ACCOUNT": "CDPDeployer",
        },
    )
    _write_env(
        l2e,
        l2_fork_env,
        l2_rpc,
        {
            "L2_RECEIVER": l2a.get("l2Receiver", ""),
            "L2_TSA": l2a.get("l2Tsa", ""),
            "OUTPUT_JSON": str(l2_out.relative_to(ROOT_DIR)),
            "ACCOUNT": "CDPDeployer",
        },
    )

    l1_vault = l1a["l1Vault"]
    l2_receiver = l2a["l2Receiver"]

    keeper_role = run(["cast", "keccak", "KEEPER_ROLE"]).strip()
    rfq_role = run(["cast", "keccak", "RFQ_SIGNER_ROLE"]).strip()

    try:
        l1_admin = _find_default_admin(
            l1_rpc,
            l1_vault,
            [l1e.get("ADMIN", ""), l1e.get("PROXY_ADMIN", ""), "0x43F4600D98Ae531D7e5F1f8FF68ef97779d31641", ANVIL_ADDR0],
        )
        _grant_role_if_needed(l1_rpc, l1_vault, keeper_role, ANVIL_ADDR0, l1_admin)
        _grant_role_if_needed(l1_rpc, l1_vault, rfq_role, ANVIL_ADDR0, l1_admin)
    except Exception as exc:
        typer.echo(f"[warn] skipped L1 role setup: {exc}")

    try:
        l2_admin = _find_default_admin(
            l2_rpc,
            l2_receiver,
            [l2e.get("ADMIN", ""), l2e.get("PROXY_ADMIN", ""), "0x0A3dD8C081c48c3B839c02A24faeab1f87b23560", ANVIL_ADDR0],
        )
        _grant_role_if_needed(l2_rpc, l2_receiver, keeper_role, ANVIL_ADDR0, l2_admin)
    except Exception as exc:
        typer.echo(f"[warn] skipped L2 role setup: {exc}")

    k2 = run([
        sys.executable,
        str(ROOT_DIR / "ops/management/l2_keeper_handle_messages.py"),
        str(l2_fork_env),
        "--once",
        "--broadcast",
        "--private-key",
        ANVIL_PK0,
        "--json",
    ])
    k1 = run([
        sys.executable,
        str(ROOT_DIR / "ops/management/l1_keeper_handle_messages.py"),
        str(l1_fork_env),
        "--once",
        "--broadcast",
        "--private-key",
        ANVIL_PK0,
        "--logs-rpc-url",
        l1_rpc,
        "--json",
    ])

    m2 = run([sys.executable, str(ROOT_DIR / "ops/preflight/l2_message_preflight.py"), str(l2_fork_env), "--json"])
    m1 = run([
        sys.executable,
        str(ROOT_DIR / "ops/management/l1_message_preflight.py"),
        str(l1_fork_env),
        "--json",
        "--logs-rpc-url",
        l1_rpc,
    ])

    report = {
        "tmpDir": str(tmpdir),
        "l1ForkEnv": str(l1_fork_env),
        "l2ForkEnv": str(l2_fork_env),
        "l1OutputJson": str(l1_out),
        "l2OutputJson": str(l2_out),
        "l1Addrs": l1a,
        "l2Addrs": l2a,
        "l2Keeper": _loads_json_relaxed(k2),
        "l1Keeper": _loads_json_relaxed(k1),
        "l2Messages": _loads_json_relaxed(m2),
        "l1Messages": _loads_json_relaxed(m1),
    }
    if json_output:
        print(json.dumps(report, indent=2))
    else:
        _print_human_report(report)

    if not keep_anvil:
        p1.terminate()
        p2.terminate()


if __name__ == "__main__":
    app()
