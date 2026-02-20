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


def sign_hash_no_prefix(digest_hex: str) -> str:
    return run(["cast", "wallet", "sign", "--no-hash", "--private-key", ANVIL_PK0, digest_hex])


def _keccak_text(text: str) -> str:
    return run(["cast", "keccak", text]).splitlines()[0].strip()


def _keccak_hex(hex_data: str) -> str:
    return run(["cast", "keccak", hex_data]).splitlines()[0].strip()


def _abi_encode(sig: str, *args: str) -> str:
    return run(["cast", "abi-encode", sig, *args]).splitlines()[0].strip()


def sign_permit2_single(chain_id: int, permit2: str, token: str, amount: int, expiration: int, nonce: int, spender: str, sig_deadline: int) -> str:
    domain_typehash = _keccak_text("EIP712Domain(string name,uint256 chainId,address verifyingContract)")
    name_hash = _keccak_text("Permit2")
    domain_sep = _keccak_hex(_abi_encode("f(bytes32,bytes32,uint256,address)", domain_typehash, name_hash, str(chain_id), permit2))

    details_typehash = _keccak_text("PermitDetails(address token,uint160 amount,uint48 expiration,uint48 nonce)")
    details_hash = _keccak_hex(
        _abi_encode(
            "f(bytes32,address,uint160,uint48,uint48)",
            details_typehash,
            token,
            str(amount),
            str(expiration),
            str(nonce),
        )
    )

    single_typehash = _keccak_text("PermitSingle(PermitDetails details,address spender,uint256 sigDeadline)PermitDetails(address token,uint160 amount,uint48 expiration,uint48 nonce)")
    struct_hash = _keccak_hex(_abi_encode("f(bytes32,bytes32,address,uint256)", single_typehash, details_hash, spender, str(sig_deadline)))

    prefix = "0x1901"
    digest = _keccak_hex(prefix + domain_sep[2:] + struct_hash[2:])
    return sign_hash_no_prefix(digest)


def ensure_token_balance_via_faucet(rpc: str, faucet: str, token: str, to: str, amount: int) -> str:
    bal_before = int(cast_call(rpc, token, "balanceOf(address)(uint256)", to).split()[0])
    if bal_before >= amount:
        return "already-funded"

    cast_send(rpc, faucet, "getTokens(address,address[])", to, f"[{token}]")

    bal_after = int(cast_call(rpc, token, "balanceOf(address)(uint256)", to).split()[0])
    if bal_after >= amount:
        return "funded-via-faucet"

    raise RuntimeError("faucet funding failed for required collateral token amount")


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
        faucet = l1_env["FAUCET"]
        permit2 = cast_call(l1_rpc, vault, "permit2()(address)").splitlines()[0].strip()
        next_loan = int(cast_call(l1_rpc, vault, "nextLoanId()(uint256)").split()[0])

        fund_mode = ensure_token_balance_via_faucet(l1_rpc, faucet, weth, ANVIL_ADDR0, 10**18)
        cast_send(l1_rpc, weth, "approve(address,uint256)", permit2, str(2**256 - 1))

        chain_id = int(run(["cast", "chain-id", "--rpc-url", l1_rpc]))
        now = int(time.time())
        expiration = now + 3600
        sig_deadline = now + 3600
        allow_raw = cast_call(l1_rpc, permit2, "allowance(address,address,address)(uint160,uint48,uint48)", ANVIL_ADDR0, weth, vault)
        nonce = int(allow_raw.splitlines()[2].split()[0])
        sig = sign_permit2_single(chain_id, permit2, weth, 10**18, expiration, nonce, vault, sig_deadline)

        maturity = now + 7 * 24 * 3600
        params = f"({weth},1000000000000000000,{maturity},1500000000000000000000,1500000000)"
        permit_arg = f"(({weth},1000000000000000000,{expiration},{nonce}),{vault},{sig_deadline})"

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
