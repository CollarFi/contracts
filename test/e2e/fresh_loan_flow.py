#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

import typer
from eth_abi import decode

ROOT = Path(__file__).resolve().parents[2]
ANVIL_PK0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDR0 = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
# Use a dedicated borrower signer so Permit2 treats signer as EOA (not EIP-1271 contract).
BORROWER_PK = "0x59c6995e998f97a5a0044966f0945382d77ad9e6f3c6f7f8b8d7a0f4f7f9d6f1"

app = typer.Typer(add_completion=False)


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout.strip()


def cast_call(rpc: str, to: str, sig: str, *args: str) -> str:
    return run(["cast", "call", to, sig, *args, "--rpc-url", rpc])


def cast_send(rpc: str, to: str, sig: str, *args: str, value: str | None = None, private_key: str = ANVIL_PK0) -> str:
    cmd = ["cast", "send", to, sig, *args, "--rpc-url", rpc, "--private-key", private_key]
    if value:
        cmd += ["--value", value]
    return run(cmd)


def sign_hash_no_prefix(digest_hex: str, private_key: str) -> str:
    return run(["cast", "wallet", "sign", "--no-hash", "--private-key", private_key, digest_hex])


def _keccak_text(text: str) -> str:
    return run(["cast", "keccak", text]).splitlines()[0].strip()


def _keccak_hex(hex_data: str) -> str:
    return run(["cast", "keccak", hex_data]).splitlines()[0].strip()


def _abi_encode(sig: str, *args: str) -> str:
    return run(["cast", "abi-encode", sig, *args]).splitlines()[0].strip()


def sign_permit2_single(chain_id: int, permit2: str, token: str, amount: int, expiration: int, nonce: int, spender: str, sig_deadline: int, private_key: str) -> str:
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
    return sign_hash_no_prefix(digest, private_key)


def wallet_address(private_key: str) -> str:
    return run(["cast", "wallet", "address", "--private-key", private_key]).strip()


def ensure_token_balance_via_faucet(rpc: str, faucet: str, token: str, to: str, amount: int) -> str:
    bal_before = int(cast_call(rpc, token, "balanceOf(address)(uint256)", to).split()[0])
    if bal_before >= amount:
        return "already-funded"

    cast_send(rpc, faucet, "getTokens(address,address[])", to, f"[{token}]")

    bal_after = int(cast_call(rpc, token, "balanceOf(address)(uint256)", to).split()[0])
    if bal_after >= amount:
        return "funded-via-faucet"

    raise RuntimeError("faucet funding failed for required collateral token amount")


def _cast_rpc(rpc: str, method: str, params: dict) -> dict:
    return json.loads(run(["cast", "rpc", method, json.dumps(params), "--rpc-url", rpc]))


def _iter_calls(node: dict):
    yield node
    for c in node.get("calls", []) or []:
        yield from _iter_calls(c)


def _extract_send_packet_from_tx(src_rpc: str, tx_hash: str) -> dict:
    trace = _cast_rpc(
        src_rpc,
        "debug_traceTransaction",
        {"txHash": tx_hash, "tracer": "callTracer", "tracerConfig": {"withLog": False}},
    )

    for call in _iter_calls(trace):
        data = (call.get("input") or "").lower()
        if data.startswith("0x4389e58f"):
            payload = bytes.fromhex(data[10:])
            params, _options, _pay_in_lz = decode(["(uint64,uint32,address,uint32,bytes32,bytes32,bytes)", "bytes", "bool"], payload)
            nonce, src_eid, sender, dst_eid, receiver_b32, guid, message = params
            receiver_addr = "0x" + receiver_b32[-20:].hex()
            sender_b32 = "0x" + ("00" * 12) + sender.lower().removeprefix("0x")
            return {
                "nonce": int(nonce),
                "srcEid": int(src_eid),
                "dstEid": int(dst_eid),
                "sender": sender.lower(),
                "senderB32": sender_b32,
                "receiver": receiver_addr,
                "guid": "0x" + guid.hex(),
                "message": "0x" + message.hex(),
            }

    raise RuntimeError(f"send((...),bytes,bool) call not found in tx trace: {tx_hash}")


def relay_exact_lz_packet(src_rpc: str, dst_rpc: str, tx_hash: str) -> dict:
    pkt = _extract_send_packet_from_tx(src_rpc, tx_hash)
    endpoint = cast_call(dst_rpc, pkt["receiver"], "endpoint()(address)").splitlines()[0].strip()

    run(["cast", "rpc", "anvil_setBalance", endpoint, "0x3635C9ADC5DEA00000", "--rpc-url", dst_rpc])

    tx = run(
        [
            "cast",
            "send",
            pkt["receiver"],
            "lzReceive((uint32,bytes32,uint64),bytes32,bytes,address,bytes)",
            f"({pkt['srcEid']},{pkt['senderB32']},{pkt['nonce']})",
            pkt["guid"],
            pkt["message"],
            "0x0000000000000000000000000000000000000000",
            "0x",
            "--rpc-url",
            dst_rpc,
            "--unlocked",
            "--from",
            endpoint,
            "--gas-limit",
            "1500000",
        ]
    )
    pkt["relayTx"] = tx
    pkt["endpoint"] = endpoint
    return pkt


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
        borrower = wallet_address(BORROWER_PK)
        # borrower balance + faucet funding + permit2 approval
        run(["cast", "rpc", "anvil_setBalance", borrower, "0x3635C9ADC5DEA00000", "--rpc-url", l1_rpc])
        code = run(["cast", "code", borrower, "--rpc-url", l1_rpc]).strip().lower()
        if code != "0x":
            raise RuntimeError(f"borrower address has code on fork (not EOA for Permit2): {borrower}")

        l1_env = {}
        for line in (ROOT / ".env.l1.testnet").read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                l1_env[k.strip()] = v.strip()
        weth = l1_env["WETH_ASSET"]
        faucet = l1_env["FAUCET"]
        permit2 = cast_call(l1_rpc, vault, "permit2()(address)").splitlines()[0].strip()
        next_loan = int(cast_call(l1_rpc, vault, "nextLoanId()(uint256)").split()[0])

        fund_mode = ensure_token_balance_via_faucet(l1_rpc, faucet, weth, borrower, 10**18)
        cast_send(l1_rpc, weth, "approve(address,uint256)", permit2, str(2**256 - 1), private_key=BORROWER_PK)

        chain_id = int(run(["cast", "chain-id", "--rpc-url", l1_rpc]))
        now = int(time.time())
        expiration = now + 3600
        sig_deadline = now + 3600
        allow_raw = cast_call(l1_rpc, permit2, "allowance(address,address,address)(uint160,uint48,uint48)", borrower, weth, vault)
        nonce = int(allow_raw.splitlines()[2].split()[0])
        sig = sign_permit2_single(chain_id, permit2, weth, 10**18, expiration, nonce, vault, sig_deadline, BORROWER_PK)

        maturity = now + 7 * 24 * 3600
        params = f"({weth},1000000000000000000,{maturity},1500000000000000000000,1500000000)"
        permit_arg = f"(({weth},1000000000000000000,{expiration},{nonce}),{vault},{sig_deadline})"

        # Quote exact native value needed: bridge fee + LZ fee.
        l2_recipient = cast_call(l1_rpc, vault, "l2Recipient()(address)").splitlines()[0].strip()
        bridge_fee = int(cast_call(l1_rpc, vault, "estimateBridgeFees(address,address,uint256)(uint256)", weth, l2_recipient, str(10**18)).split()[0])
        lz_messenger = cast_call(l1_rpc, vault, "lzMessenger()(address)").splitlines()[0].strip()
        l2_asset = cast_call(l1_rpc, vault, "l2MessageAsset(address)(address)", weth).splitlines()[0].strip()
        subaccount_id = int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0])
        default_opts = cast_call(l1_rpc, lz_messenger, "defaultOptions()(bytes)").splitlines()[0].strip()
        quote_msg = f"(0,{next_loan},{l2_asset},1000000000000000000,{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,0x)"
        lz_fee_raw = cast_call(l1_rpc, lz_messenger, "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))", quote_msg, default_opts)
        m = re.search(r"\d+", lz_fee_raw)
        if not m:
            raise RuntimeError(f"unable to parse LZ fee from quote: {lz_fee_raw}")
        lz_fee = int(m.group(0))
        total_value = bridge_fee + lz_fee

        tx = cast_send(
            l1_rpc,
            vault,
            "createDepositWithPermit((address,uint256,uint256,uint256,uint256),((address,uint160,uint48,uint48),address,uint256),bytes)",
            params,
            permit_arg,
            sig,
            value=str(total_value),
            private_key=BORROWER_PK,
        )
        return {"tx": tx, "loanId": next_loan, "fundMode": fund_mode, "borrower": borrower, "bridgeFee": bridge_fee, "lzFee": lz_fee}

    step("create_deposit_with_permit", do_deposit)

    deposit_tx = out["steps"][-1]["result"]["tx"]

    step("relay_l1_to_l2_exact", lambda: relay_exact_lz_packet(l1_rpc, l2_rpc, deposit_tx))

    def run_l2_keeper_once():
        base = (ROOT / ".env.l2.testnet").read_text()
        tmp = Path(tempfile.mkdtemp(prefix="e2e-l2-")) / ".env.l2.fork"
        tmp.write_text(base + f"\nRPC_URL={l2_rpc}\nL2_RECEIVER={receiver}\n")
        return json.loads(run([
            "uv", "run", "python", str(ROOT / "ops/management/l2_keeper_handle_messages.py"),
            str(tmp), "--once", "--broadcast", "--private-key", ANVIL_PK0, "--json"
        ]))

    step("l2_keeper_handle_deposit", run_l2_keeper_once)

    def relay_l2_ack_to_l1():
        k = out["steps"][-1]["result"]
        handled = k.get("handled", [])
        sent = [h for h in handled if h.get("status") == "sent" and h.get("tx")]
        if not sent:
            raise RuntimeError(f"no sent ack tx in keeper output: {json.dumps(k)}")
        return relay_exact_lz_packet(l2_rpc, l1_rpc, sent[0]["tx"])

    step("relay_l2_to_l1_exact", relay_l2_ack_to_l1)

    out["ok"] = True
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    app()
