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
sys.path.insert(0, str(ROOT_DIR / "ops"))
from lz_harness.common import load_env, run  # noqa: E402

app = typer.Typer(add_completion=False)

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


def _loads_json_relaxed(raw: str) -> dict:
    return json.loads(raw, strict=False)


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


@app.command()
def main(
    l1_json: Path = typer.Option(..., help="L1 deployment json (e.g. deployments/421614/l1.json)"),
    l2_json: Path = typer.Option(..., help="L2 deployment json (e.g. deployments/901/l2.json)"),
    l1_env: Path = typer.Option(ROOT_DIR / ".env.l1.testnet"),
    l2_env: Path = typer.Option(ROOT_DIR / ".env.l2.testnet"),
    l1_port: int = typer.Option(8758),
    l2_port: int = typer.Option(8759),
    l1_rpc: str = typer.Option("", help="Use existing L1 RPC (skip spawning anvil for L1)"),
    l2_rpc: str = typer.Option("", help="Use existing L2 RPC (skip spawning anvil for L2)"),
    keep_anvil: bool = typer.Option(False, help="Keep anvil processes running"),
) -> None:
    l1a = _read_addrs(l1_json)
    l2a = _read_addrs(l2_json)
    l1e = load_env(l1_env)
    l2e = load_env(l2_env)

    p1 = None
    p2 = None
    l1_rpc_url = l1_rpc or f"http://127.0.0.1:{l1_port}"
    l2_rpc_url = l2_rpc or f"http://127.0.0.1:{l2_port}"

    if not l1_rpc:
        p1 = _spawn_anvil(l1e["RPC_URL"], l1_port, int(l1_json.parent.name))
    if not l2_rpc:
        p2 = _spawn_anvil(l2e["RPC_URL"], l2_port, int(l2_json.parent.name))
    if p1 or p2:
        time.sleep(1.2)

    tmpdir = Path(tempfile.mkdtemp(prefix="collar-e2e-"))
    l1_fork_env = tmpdir / "l1.fork.env"
    l2_fork_env = tmpdir / "l2.fork.env"

    _write_env(
        l1e,
        l1_fork_env,
        l1_rpc_url,
        {
            "L1_VAULT": l1a.get("l1Vault", ""),
            "L1_MESSENGER": l1a.get("l1Messenger", ""),
            "ACCOUNT": "CDPDeployer",
        },
    )
    _write_env(
        l2e,
        l2_fork_env,
        l2_rpc_url,
        {
            "L2_RECEIVER": l2a.get("l2Receiver", ""),
            "L2_TSA": l2a.get("l2Tsa", ""),
            "ACCOUNT": "CDPDeployer",
        },
    )

    # role setup (fork-only) using known admin from current deployments (borrower/admin address)
    l1_vault = l1a["l1Vault"]
    l2_receiver = l2a["l2Receiver"]
    l2_tsa = l2a["l2Tsa"]

    keeper_role = run(["cast", "keccak", "KEEPER_ROLE"]).strip()
    rfq_role = run(["cast", "keccak", "RFQ_SIGNER_ROLE"]).strip()

    l1_rpc = l1_rpc_url
    l2_rpc = l2_rpc_url

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

    # L2 then L1 keeper once
    k2 = run([
        "uv",
        "run",
        "python",
        str(ROOT_DIR / "ops/management/l2_keeper_handle_messages.py"),
        str(l2_fork_env),
        "--once",
        "--broadcast",
        "--private-key",
        ANVIL_PK0,
        "--json",
    ])
    k1 = run([
        "uv",
        "run",
        "python",
        str(ROOT_DIR / "ops/management/l1_keeper_handle_messages.py"),
        str(l1_fork_env),
        "--once",
        "--broadcast",
        "--private-key",
        ANVIL_PK0,
        "--logs-rpc-url",
        l1_rpc_url,
        "--json",
    ])

    m2 = run(["uv", "run", "python", str(ROOT_DIR / "ops/preflight/l2_message_preflight.py"), str(l2_fork_env), "--json"])
    m1 = run([
        "uv",
        "run",
        "python",
        str(ROOT_DIR / "ops/management/l1_message_preflight.py"),
        str(l1_fork_env),
        "--json",
        "--logs-rpc-url",
        l1_rpc_url,
    ])

    report = {
        "tmpDir": str(tmpdir),
        "l1ForkEnv": str(l1_fork_env),
        "l2ForkEnv": str(l2_fork_env),
        "l2Keeper": _loads_json_relaxed(k2),
        "l1Keeper": _loads_json_relaxed(k1),
        "l2Messages": _loads_json_relaxed(m2),
        "l1Messages": _loads_json_relaxed(m1),
    }
    print(json.dumps(report, indent=2))

    if not keep_anvil:
        if p1 is not None:
            p1.terminate()
        if p2 is not None:
            p2.terminate()


if __name__ == "__main__":
    app()
