#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[2]
ANVIL_PK0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDR0 = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"

app = typer.Typer(add_completion=False)


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout.strip()


def cast_call(rpc: str, to: str, sig: str, *args: str) -> str:
    return run(["cast", "call", to, sig, *args, "--rpc-url", rpc])


def cast_send(rpc: str, to: str, sig: str, *args: str, value: str | None = None) -> str:
    cmd = ["cast", "send", to, sig, *args, "--rpc-url", rpc, "--private-key", ANVIL_PK0]
    if value:
        cmd += ["--value", value]
    return run(cmd)


def sign_typed_data(typed: dict) -> str:
    return run(["cast", "wallet", "sign", "--data", "--private-key", ANVIL_PK0, json.dumps(typed)])


def ensure_token_balance(rpc: str, token: str, to: str, amount: int) -> str:
    bal = int(cast_call(rpc, token, "balanceOf(address)(uint256)", to).split()[0])
    if bal >= amount:
        return "already-funded"
    try:
        cast_send(rpc, token, "deposit()", value=str(amount - bal))
        return "funded-via-deposit"
    except Exception:
        pass

    logs = run([
        "cast",
        "logs",
        "Transfer(address,address,uint256)",
        "--address",
        token,
        "--from-block",
        "0",
        "--to-block",
        "latest",
        "--rpc-url",
        rpc,
        "--json",
    ])
    entries = json.loads(logs)
    seen: set[str] = set()
    for e in reversed(entries[-2000:]):
        t = e.get("topics", [])
        if len(t) < 3:
            continue
        holder = "0x" + t[1][-40:]
        if holder.lower() in seen:
            continue
        seen.add(holder.lower())
        try:
            hbal = int(cast_call(rpc, token, "balanceOf(address)(uint256)", holder).split()[0])
            if hbal >= amount:
                run(["cast", "rpc", "anvil_setBalance", holder, "0x56BC75E2D63100000", "--rpc-url", rpc])
                run([
                    "cast", "send", token, "transfer(address,uint256)", to, str(amount),
                    "--rpc-url", rpc, "--unlocked", "--from", holder
                ])
                return f"funded-via-holder:{holder}"
        except Exception:
            continue
    raise RuntimeError("unable to fund borrower with collateral token on fork")


@app.command()
def main(
    l1_json: Path = typer.Option(Path("deployments/421614/l1-clean.json")),
    l2_json: Path = typer.Option(Path("deployments/901/l2-clean.json")),
    l1_rpc: str = typer.Option("http://127.0.0.1:8868"),
    l2_rpc: str = typer.Option("http://127.0.0.1:8869"),
):
    l1 = json.loads((ROOT / l1_json).read_text())
    l2 = json.loads((ROOT / l2_json).read_text())
    l1a = l1.get("addrs", l1)
    l2a = l2.get("addrs", l2)

    vault = l1a["l1Vault"]
    receiver = l2a["l2Receiver"]

    out: dict = {"ok": False, "steps": []}

    def step(name: str, fn):
        rec = {"step": name, "ok": False}
        try:
            rec["result"] = fn()
            rec["ok"] = True
        except Exception as e:
            rec["error"] = str(e)
            out["steps"].append(rec)
            print(json.dumps(out, indent=2))
            raise SystemExit(1)
        out["steps"].append(rec)

    step("grant_l2_keeper", lambda: cast_send(l2_rpc, receiver, "grantRole(bytes32,address)", run(["cast", "keccak", "KEEPER_ROLE"]), ANVIL_ADDR0))

    def do_deposit():
        # borrower balance + weth wrap + permit2 approval
        run(["cast", "rpc", "anvil_setBalance", ANVIL_ADDR0, "0x3635C9ADC5DEA00000", "--rpc-url", l1_rpc])
        l1_env = {}
        for line in (ROOT / ".env.l1.testnet").read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                l1_env[k.strip()] = v.strip()
        weth = l1_env["WETH_ASSET"]
        permit2 = cast_call(l1_rpc, vault, "permit2()(address)").splitlines()[0].strip()
        next_loan = int(cast_call(l1_rpc, vault, "nextLoanId()(uint256)").split()[0])

        fund_mode = ensure_token_balance(l1_rpc, weth, ANVIL_ADDR0, 10**18)
        cast_send(l1_rpc, weth, "approve(address,uint256)", permit2, str(2**256 - 1))

        # Permit2 typed data
        chain_id = int(run(["cast", "chain-id", "--rpc-url", l1_rpc]))
        now = int(time.time())
        permit = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "PermitDetails": [
                    {"name": "token", "type": "address"},
                    {"name": "amount", "type": "uint160"},
                    {"name": "expiration", "type": "uint48"},
                    {"name": "nonce", "type": "uint48"},
                ],
                "PermitSingle": [
                    {"name": "details", "type": "PermitDetails"},
                    {"name": "spender", "type": "address"},
                    {"name": "sigDeadline", "type": "uint256"},
                ],
            },
            "primaryType": "PermitSingle",
            "domain": {"name": "Permit2", "chainId": chain_id, "verifyingContract": permit2},
            "message": {
                "details": {"token": weth, "amount": str(10**18), "expiration": str(now + 3600), "nonce": "0"},
                "spender": vault,
                "sigDeadline": str(now + 3600),
            },
        }
        sig = sign_typed_data(permit)

        maturity = now + 7 * 24 * 3600
        params = f"({weth},1000000000000000000,{maturity},1500000000000000000000,1500000000)"
        permit_arg = f"(({weth},1000000000000000000,{now+3600},0),{vault},{now+3600})"

        tx = cast_send(
            l1_rpc,
            vault,
            "createDepositWithPermit((address,uint256,uint256,uint256,uint256),((address,uint160,uint48,uint48),address,uint256),bytes)",
            params,
            permit_arg,
            sig,
            value="20000000000000000",
        )
        return {"tx": tx, "loanId": next_loan, "fundMode": fund_mode}

    step("create_deposit_with_permit", do_deposit)

    loan_id = out["steps"][-1]["result"]["loanId"]

    def run_l2_keeper_once():
        base = (ROOT / ".env.l2.testnet").read_text()
        tmp = Path(tempfile.mkdtemp(prefix="e2e-l2-")) / ".env.l2.fork"
        tmp.write_text(base + f"\nRPC_URL={l2_rpc}\nL2_RECEIVER={receiver}\n")
        return run([
            "uv", "run", "python", str(ROOT / "ops/management/l2_keeper_handle_messages.py"),
            str(tmp), "--once", "--broadcast", "--private-key", ANVIL_PK0, "--json"
        ])

    step("l2_keeper_handle_deposit", run_l2_keeper_once)

    out["ok"] = True
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    app()
